"""Discussione sulle richieste di merge: commenti a thread, con eventi WebSocket, audit e conteggio per le liste.

Endpoint (tutti sotto ``/api/projects/{pid}``):
  GET    /proposals/{prop}/comments   Viewer+      elenco cronologico (il thread si ricostruisce con ``reply_to``)
  POST   /proposals/{prop}/comments   Developer+   nuovo commento (anche in risposta a un altro, con ``anchor`` facoltativo)
  PATCH  /comments/{cid}              solo l'autore, entro 15 minuti dalla pubblicazione
  DELETE /comments/{cid}              autore o Admin: eliminazione "morbida" (il testo diventa «[commento eliminato]»)

Le richieste di merge vivono in memoria nel SessionManager: l'esistenza della richiesta si verifica li'. I commenti di
richieste sparite (riavvio del server) restano nel database ma non sono raggiungibili dall'API.
I commenti di un progetto spariscono insieme al progetto (``ProjectRegistry.delete``); l'export .zip li ignora.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy import func, select

from common.roles import Role
from common.schemas import Event
from common.schemas_comments import (DELETED_TEXT, EDIT_WINDOW_S, MAX_COMMENT_LEN, CommentEdit, CommentIn,
                                     CommentOut, find_mentions)
from server.auth.rbac import effective_project_role
from server.db.base import Database
from server.db.models import User
from server.db.models_comments import ProposalComment


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CommentService:
    """Accesso ai commenti + contatore in memoria dei non eliminati (una sola query aggregata per progetto)."""

    def __init__(self, db: Database, clock: Callable[[], datetime] = _utcnow):
        self.db, self.clock = db, clock
        self._counts: dict[int, dict[str, int]] = {}
        self._lock = threading.Lock()

    # ---- contatori
    def count(self, project_id: int, proposal_id: str) -> int:
        with self._lock:
            c = self._counts.get(project_id)
        if c is None:
            with self.db.scope() as s:
                rows = s.execute(select(ProposalComment.proposal_id, func.count()).where(
                    ProposalComment.project_id == project_id, ProposalComment.deleted.is_(False))
                    .group_by(ProposalComment.proposal_id)).all()
            c = dict(rows)
            with self._lock:
                c = self._counts.setdefault(project_id, c)
        return c.get(proposal_id, 0)

    def _bump(self, project_id: int, proposal_id: str, delta: int) -> None:
        self.count(project_id, proposal_id)                       # garantisce che la cache esista
        with self._lock:
            c = self._counts.setdefault(project_id, {})
            c[proposal_id] = max(0, c.get(proposal_id, 0) + delta)

    def forget(self, project_id: int) -> None:
        with self._lock:
            self._counts.pop(project_id, None)

    @staticmethod
    def delete_rows(session, project_id: int) -> int:               # noqa: ANN001
        """Cancella i commenti di un progetto nella transazione di chi chiama (eliminazione del progetto)."""
        return session.query(ProposalComment).filter_by(project_id=project_id).delete()

    # ---- menzioni
    def valid_mentions(self, project_id: int, names: Iterable[str]) -> set[str]:
        names = {n.lower() for n in names}
        if not names:
            return set()
        ok = set()
        with self.db.scope() as s:
            for u in s.scalars(select(User).where(User.username.in_(names), User.is_active.is_(True))):
                if effective_project_role(s, u, project_id) is not None:
                    ok.add(u.username)
        return ok

    # ---- operazioni
    def list(self, project_id: int, proposal_id: str) -> list[CommentOut]:
        with self.db.scope() as s:
            rows = list(s.scalars(select(ProposalComment).where(
                ProposalComment.project_id == project_id, ProposalComment.proposal_id == proposal_id)
                .order_by(ProposalComment.ts, ProposalComment.id)))
        valid = self.valid_mentions(project_id, {n for r in rows if not r.deleted for n in find_mentions(r.text)})
        return [self.out(r, valid) for r in rows]

    @staticmethod
    def out(r: ProposalComment, valid: Optional[set[str]] = None) -> CommentOut:
        ment = [] if r.deleted or valid is None else [n for n in find_mentions(r.text) if n in valid]
        return CommentOut(id=r.id, project_id=r.project_id, proposal_id=r.proposal_id, author=r.author,
                          author_role=r.author_role, ts=datetime.fromisoformat(r.ts), text=r.text,
                          reply_to=r.reply_to, anchor=r.anchor,
                          edited_at=datetime.fromisoformat(r.edited_at) if r.edited_at else None,
                          deleted=bool(r.deleted), mentions=ment)

    def add(self, project_id: int, proposal_id: str, author: str, role: str, body: CommentIn) -> CommentOut:
        text = _clean_text(body.text)
        anchor = (body.anchor or "").strip()[:400] or None
        with self.db.scope() as s:
            if body.reply_to is not None:
                parent = s.get(ProposalComment, body.reply_to)
                if parent is None or parent.project_id != project_id or parent.proposal_id != proposal_id:
                    raise HTTPException(422, "Il commento a cui rispondi non appartiene a questa richiesta")
            row = ProposalComment(project_id=project_id, proposal_id=proposal_id, author=author, author_role=role,
                                  ts=self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds"),
                                  text=text, reply_to=body.reply_to, anchor=anchor, deleted=False)
            s.add(row)
            s.flush()
            s.expunge(row)
        self._bump(project_id, proposal_id, +1)
        return self.out(row, self.valid_mentions(project_id, find_mentions(text)))

    def _get(self, s, project_id: int, cid: int) -> ProposalComment:                 # noqa: ANN001
        row = s.get(ProposalComment, cid)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "Commento inesistente")
        return row

    def edit(self, project_id: int, cid: int, user: str, text: str) -> CommentOut:
        text = _clean_text(text)
        with self.db.scope() as s:
            row = self._get(s, project_id, cid)
            if row.author != user:
                raise HTTPException(403, "Puoi modificare solo i tuoi commenti")
            if row.deleted:
                raise HTTPException(409, "Il commento e' stato eliminato")
            now = self.clock()
            if (now - datetime.fromisoformat(row.ts)).total_seconds() > EDIT_WINDOW_S:
                raise HTTPException(403, f"Un commento si puo' modificare solo entro {EDIT_WINDOW_S // 60} minuti")
            row.text = text
            row.edited_at = now.astimezone(timezone.utc).isoformat(timespec="microseconds")
            s.flush()
            s.expunge(row)
        return self.out(row, self.valid_mentions(project_id, find_mentions(text)))

    def delete(self, project_id: int, cid: int, user: str, is_admin: bool) -> tuple[CommentOut, bool]:
        """Soft delete. Ritorna (commento, True se e' stato eliminato adesso)."""
        with self.db.scope() as s:
            row = self._get(s, project_id, cid)
            if row.author != user and not is_admin:
                raise HTTPException(403, "Puoi eliminare solo i tuoi commenti (o essere Admin)")
            changed = not row.deleted
            if changed:
                row.deleted, row.text = True, DELETED_TEXT
            s.flush()
            s.expunge(row)
        if changed:
            self._bump(project_id, row.proposal_id, -1)
        return self.out(row), changed


def _clean_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise HTTPException(422, "Il commento e' vuoto")
    if len(t) > MAX_COMMENT_LEN:
        raise HTTPException(422, f"Commento troppo lungo (massimo {MAX_COMMENT_LEN} caratteri)")
    return t


def mount_comments(app: FastAPI, ctx: Callable[[Role], Callable], registry, audit) -> CommentService:
    """Registra le route dei commenti; ``ctx`` e' la factory di contesto di progetto di ``create_app``."""
    svc: CommentService = registry.comments
    app.state.comments = svc
    r = APIRouter(prefix="/api/projects/{pid}", tags=["commenti"])

    def rec(c, action: str, target: str, **details) -> None:                         # noqa: ANN001
        if getattr(c, "branch", None):
            details = {**details, "branch": c.branch}
        audit.record(action, c.access.principal.username, project_id=c.access.project_id, target=target,
                     ip=c.access.principal.ip, details=details)

    def emit(c, typ: str, prop: str, out: CommentOut, verb: str, mentions: Iterable[str] = ()) -> None:   # noqa: ANN001
        mgr = c.rt.mgr
        try:
            summary = mgr.summary(mgr.get(prop))
        except Exception:                                                              # noqa: BLE001
            summary = None
        mgr.sink.publish(Event(type=typ, message=f"{c.access.principal.username} {verb} la richiesta #{prop}",
                               proposal=summary, developer=c.access.principal.username,
                               mentions=list(mentions)).model_dump(mode="json"), None)

    @r.get("/proposals/{prop}/comments", response_model=list[CommentOut])
    def list_comments(prop: str, c=Depends(ctx(Role.VIEWER))):
        c.rt.mgr.get(prop)                                                             # 404 se la richiesta non esiste
        return svc.list(c.access.project_id, prop)

    @r.post("/proposals/{prop}/comments", response_model=CommentOut, status_code=201)
    def add_comment(prop: str, body: CommentIn, c=Depends(ctx(Role.DEVELOPER))):
        c.rt.mgr.get(prop)
        out = svc.add(c.access.project_id, prop, c.access.principal.username, c.access.role.key, body)
        rec(c, "comment_added", prop, comment_id=out.id, preview=out.text[:80], reply_to=out.reply_to)
        emit(c, "comment_added", prop, out, "ha commentato", out.mentions)
        return out

    @r.patch("/comments/{cid}", response_model=CommentOut)
    def edit_comment(cid: int, body: CommentEdit, c=Depends(ctx(Role.DEVELOPER))):
        out = svc.edit(c.access.project_id, cid, c.access.principal.username, body.text)
        rec(c, "comment_edited", out.proposal_id, comment_id=cid, preview=out.text[:80])
        emit(c, "comment_updated", out.proposal_id, out, "ha modificato un commento sulla", out.mentions)
        return out

    @r.delete("/comments/{cid}", response_model=CommentOut)
    def delete_comment(cid: int, c=Depends(ctx(Role.DEVELOPER))):
        out, changed = svc.delete(c.access.project_id, cid, c.access.principal.username, c.access.role >= Role.ADMIN)
        if changed:
            rec(c, "comment_deleted", out.proposal_id, comment_id=cid, comment_author=out.author)
            emit(c, "comment_deleted", out.proposal_id, out, "ha eliminato un commento sulla")
        return out

    app.include_router(r)
    return svc
