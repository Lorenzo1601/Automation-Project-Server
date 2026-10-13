"""Posta in arrivo (notifiche persistenti) + email opzionale.

Ogni evento importante del server (nuova richiesta di merge, richiesta rifiutata / unita, salvataggio in un branch,
menzione in un commento) diventa una riga di ``notifications`` per i destinatari giusti, anche se non sono collegati: la
trovano al prossimo accesso. Se l'utente e' collegato riceve in piu' l'evento WebSocket ``notification``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, update

from common.roles import Role
from common.schemas_inbox import MarkReadIn, NotificationOut, UnreadOut
from server.auth.models import Principal
from server.auth.rbac import get_principal
from server.config import Settings
from server.db.base import Database
from server.db.models import Project, ProjectPermission, RoleRow, User
from server.db.models_branches import Branch
from server.db.models_inbox import Notification
from server.mailer import Mailer

log = logging.getLogger("aps.inbox")
_UID = re.compile(r"u(\d+)$")


def _uid(key: str) -> Optional[int]:
    """``u12`` / ``3:u12`` / ``3:@prova:u12`` -> 12."""
    m = _UID.search(key or "")
    return int(m.group(1)) if m else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboxService:
    def __init__(self, db: Database, cfg: Settings, notifier=None, mailer: Optional[Mailer] = None):   # noqa: ANN001
        self.db, self.cfg, self.notifier = db, cfg, notifier
        self.mailer = mailer or Mailer(cfg)

    # ------------------------------------------------------------------ scrittura
    def add(self, user_ids: Iterable[int], kind: str, title: str, message: str, project_id: Optional[int] = None,
            branch: Optional[str] = None, ref: Optional[str] = None) -> list[int]:
        ids = sorted(set(user_ids))
        if not ids:
            return []
        emails: list[tuple[str, str]] = []
        with self.db.scope() as s:
            users = {u.id: u for u in s.scalars(select(User).where(User.id.in_(ids), User.is_active.is_(True)))}
            pname = ""
            if project_id is not None:
                pr = s.get(Project, project_id)
                pname = pr.name if pr else ""
            for uid, u in users.items():
                s.add(Notification(user_id=uid, project_id=project_id, ts=_now(), kind=kind, title=title[:160],
                                   message=message, branch=branch, ref=ref))
                if u.email:
                    emails.append((u.email, u.display_name or u.username))
            s.flush()
            counts = {uid: self._count(s, uid) for uid in users}
        for uid, n in counts.items():
            if self.notifier is not None:
                self.notifier.publish_user(uid, {"type": "notification", "message": f"{title}: {message}"[:300],
                                                 "revision": n, "branch": branch})
        for addr, name in emails:
            body = f"Ciao {name},\n\n{message}\n" + (f"\nProgetto: {pname}" if pname else "") + \
                   (f" · branch: {branch}" if branch else "") + "\n\n— Automation Project Server"
            self.mailer.send(addr, f"[Automation Project Server] {title}", body)
        return list(users)

    def mark_read(self, user_id: int, ids: Optional[list[int]] = None) -> int:
        with self.db.scope() as s:
            q = update(Notification).where(Notification.user_id == user_id, Notification.read.is_(False))
            if ids is not None:
                q = q.where(Notification.id.in_(ids))
            s.execute(q.values(read=True))
            return self._count(s, user_id)

    # ------------------------------------------------------------------ lettura
    @staticmethod
    def _count(s, user_id: int) -> int:                                    # noqa: ANN001
        return s.scalar(select(func.count(Notification.id)).where(Notification.user_id == user_id,
                                                                  Notification.read.is_(False))) or 0

    def unread(self, user_id: int) -> int:
        with self.db.scope() as s:
            return self._count(s, user_id)

    def list(self, user_id: int, unread_only: bool = False, limit: int = 100) -> list[NotificationOut]:
        with self.db.scope() as s:
            q = select(Notification, Project.name).outerjoin(Project, Project.id == Notification.project_id) \
                .where(Notification.user_id == user_id).order_by(Notification.id.desc()).limit(limit)
            if unread_only:
                q = q.where(Notification.read.is_(False))
            return [NotificationOut(id=n.id, ts=n.ts, kind=n.kind, title=n.title, message=n.message,
                                    project_id=n.project_id, project_name=pn or "", branch=n.branch, ref=n.ref, read=n.read)
                    for n, pn in s.execute(q).all()]

    # ------------------------------------------------------------------ da eventi
    def _leads(self, project_id: int) -> set[int]:
        """Utenti che possono dare il Consenso Finale: Admin attivi + Lead sul progetto."""
        with self.db.scope() as s:
            admins = set(s.scalars(select(User.id).join(RoleRow, RoleRow.id == User.role_id)
                                   .where(RoleRow.rank >= int(Role.ADMIN), User.is_active.is_(True))))
            leads = set(s.scalars(select(ProjectPermission.user_id).join(RoleRow, RoleRow.id == ProjectPermission.role_id)
                                  .where(ProjectPermission.project_id == project_id, RoleRow.rank >= int(Role.LEAD))))
            return admins | leads

    def _developers(self, project_id: int) -> set[int]:
        with self.db.scope() as s:
            return set(s.scalars(select(ProjectPermission.user_id).join(RoleRow, RoleRow.id == ProjectPermission.role_id)
                                 .where(ProjectPermission.project_id == project_id, RoleRow.rank >= int(Role.DEVELOPER))))

    def on_event(self, event: dict, targets: Optional[set[str]], scope: Optional[str]) -> None:
        """Chiamato da ``Notifier.publish``: trasforma gli eventi che interessano in notifiche persistenti."""
        typ = event.get("type", "")
        if typ not in ("proposal_submitted", "proposal_rejected", "proposal_merged", "branch_updated", "comment_added"):
            return
        key = scope or (next(iter(targets)) if targets else "")
        m = re.match(r"(\d+):", key or "")
        if not m:
            return
        pid, branch = int(m.group(1)), event.get("branch")
        p = event.get("proposal") or {}
        msg = event.get("message", "")
        ref = p.get("id")
        if typ == "proposal_submitted":
            who = {_uid(x["session_id"]) for x in p.get("participants", [])} - {_uid(p.get("author_session", ""))}
            if p.get("needs_lead_approval"):
                who |= self._leads(pid) - {_uid(p.get("author_session", ""))}
            self.add({u for u in who if u}, "merge_request", "Nuova richiesta di merge", msg, pid, branch, ref)
        elif typ == "proposal_rejected":
            self.add({_uid(p.get("author_session", ""))} - {None}, "merge_rejected", "Richiesta rifiutata", msg, pid, branch, ref)
        elif typ == "proposal_merged":
            who = {_uid(x["session_id"]) for x in p.get("participants", [])} | {_uid(p.get("author_session", ""))}
            self.add({u for u in who if u}, "merge_merged", "Richiesta unita al Master", msg, pid, branch, ref)
        elif typ == "branch_updated":
            actor = event.get("developer")
            slug = branch
            with self.db.scope() as s:
                b = s.scalar(select(Branch).where(Branch.project_id == pid, Branch.slug == slug)) if slug else None
                if b is None:
                    return
                owner_id = s.scalar(select(User.id).where(User.username == b.owner))
                actor_id = s.scalar(select(User.id).where(User.username == actor)) if actor else None
                kind = b.kind
            who = {owner_id} | (self._developers(pid) if kind == "team" else set())
            self.add({u for u in who if u and u != actor_id}, "branch_updated", "Branch aggiornato", msg, pid, branch)
        elif typ == "comment_added" and event.get("mentions"):
            with self.db.scope() as s:
                ids = set(s.scalars(select(User.id).where(User.username.in_([x.lower() for x in event["mentions"]]))))
                actor_id = s.scalar(select(User.id).where(User.username == event.get("developer"))) if event.get("developer") else None
            self.add({u for u in ids if u != actor_id}, "mention", "Sei stato menzionato", msg, pid, branch, ref)


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/notifications", tags=["notifiche"])

    def svc(request: Request) -> InboxService:
        return request.app.state.inbox

    @r.get("", response_model=list[NotificationOut])
    def list_notifications(request: Request, unread_only: bool = Query(False), limit: int = Query(100, ge=1, le=500),
                           p: Principal = Depends(get_principal)):
        return svc(request).list(p.user_id, unread_only, limit)

    @r.get("/count", response_model=UnreadOut)
    def count(request: Request, p: Principal = Depends(get_principal)):
        return UnreadOut(unread=svc(request).unread(p.user_id))

    @r.post("/read", response_model=UnreadOut)
    def mark_read(body: MarkReadIn, request: Request, p: Principal = Depends(get_principal)):
        return UnreadOut(unread=svc(request).mark_read(p.user_id, body.ids))

    return r
