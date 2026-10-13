"""Funzioni sulle revisioni del Master aggiunte a parte (router incluso in ``create_app``):

* confronto tra due revisioni conservate   ``GET   /master/compare?from=X&to=Y``
* etichette e blocco delle revisioni       ``PATCH /master/revisions/{n}``
* lock esclusivo (presa in carico)         ``GET/POST/DELETE /lock``, ``POST /lock/renew``
* check-in diretto dei file binari         ``POST  /master/checkin``

Decisioni (vedi README): etichetta e blocco: Lead e Admin; SBLOCCO: solo Admin; le revisioni bloccate non vengono mai
potate e non contano nel limite di revisioni (si sommano); il lock e' consultivo per i file mergeabili, per i binari
il check-in richiede di esserne l'holder.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool

from common.filekinds import KIND_LABELS, MERGEABLE, canonical_ext
from common.roles import Role
from common.schemas import ChangeOut, Event, MasterInfo
from common.schemas_revisions import (LABEL_MAX, MIN_CHECKIN_MESSAGE, MIN_LOCK_REASON, CompareOut, LockIn, LockOut,
                                      RevisionPatchIn)
from server.config import Settings
from server.db.audit import AuditService
from server.locks import LockError, LockInfo, LockManager
from server.merger import L5XError
from server.storage import RevisionPurged
from server.textmerge import merge_any

_GUARD = threading.Lock()


def locks_for(rt, cfg: Settings) -> LockManager:                                # noqa: ANN001
    """Il ``LockManager`` del progetto (uno per runtime; ``lock.json`` nella cartella dati del progetto)."""
    with _GUARD:
        lm = getattr(rt, "_locks", None)
        if lm is None:
            lm = LockManager(rt.store.root / "lock.json", lambda: cfg.lock_ttl_h * 3600.0)
            rt._locks = lm                                                      # noqa: SLF001
        return lm


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _local(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def lock_out(lm: LockManager, me: str) -> LockOut:
    cur = lm.current()
    ttl_h = round(lm.ttl / 3600.0, 4)
    if cur is None:
        return LockOut(ttl_hours=ttl_h)
    return LockOut(locked=True, holder=cur.holder, acquired_at=_dt(cur.acquired_at), expires_at=_dt(cur.expires_at),
                   note=cur.note, seconds_left=max(0, int(cur.expires_at - lm.clock())), mine=cur.holder == me,
                   ttl_hours=ttl_h)


def _held_msg(cur: LockInfo) -> str:
    return (f"Il Master e' in carico a {cur.holder} dalle {_local(cur.acquired_at)} fino alle {_local(cur.expires_at)}"
            + (f" ({cur.note})" if cur.note else ""))


def build_router(ctx: Callable[[Role], Callable], audit: AuditService, cfg: Settings) -> APIRouter:
    r = APIRouter(prefix="/api/projects/{pid}", tags=["revisioni"])

    def rec(c, action: str, target: str = "", **details) -> None:                # noqa: ANN001
        if getattr(c, "branch", None):
            details = {**details, "branch": c.branch}
        audit.record(action, c.access.principal.username, project_id=c.access.project_id, target=target,
                     ip=c.access.principal.ip, details=details)

    def announce(c, typ: str, message: str, **kw) -> None:                       # noqa: ANN001
        c.rt.mgr.sink.publish(Event(type=typ, message=message, developer=c.access.principal.username,
                                    **kw).model_dump(mode="json"), None)

    # ------------------------------------------------------------ confronto tra revisioni
    @r.get("/master/compare", response_model=CompareOut)
    def compare(from_: int = Query(..., alias="from"), to: int = Query(...), c=Depends(ctx(Role.VIEWER))):
        """Differenze da ``from`` a ``to`` (stesso formato dei cambiamenti di un merge). Entrambe devono essere conservate."""
        if from_ == to:
            raise HTTPException(422, "Scegli due revisioni diverse da confrontare")
        st = c.rt.store
        with c.rt.mgr._lock:                                                     # noqa: SLF001  (nessuna potatura mentre leggo)
            hist = st.history()
            infos = []
            for n in (from_, to):
                if not (1 <= n <= len(hist)):
                    raise HTTPException(404, f"Revisione {n} inesistente")
                if hist[n - 1].purged:
                    raise HTTPException(410, f"La revisione {n} non e' piu' conservata dal server (limite di revisioni): "
                                             "si possono confrontare solo le revisioni conservate")
                infos.append(hist[n - 1])
            a, b = infos
            if a.kind != b.kind:
                raise HTTPException(422, "Le due revisioni sono di tipo diverso: non confrontabili")
            if a.kind not in MERGEABLE:
                raise HTTPException(422, f"{KIND_LABELS.get(a.kind, 'Questo tipo di file')}: non confrontabile "
                                         "(le revisioni si possono solo scaricare)")
            blob_a, blob_b = st.read_revision(from_), st.read_revision(to)
        try:
            res = merge_any(a.kind, blob_a, blob_b, blob_a)                      # base = X, mine = Y, theirs = X
        except L5XError as exc:
            raise HTTPException(422, str(exc)) from exc
        ch = [ChangeOut(**x.__dict__) for x in res.changes]
        warn = [] if a.kind == "l5x" else [f"{KIND_LABELS[a.kind]}: confronto testuale riga per riga (non strutturale)"]
        return CompareOut(from_info=MasterInfo(**a.__dict__), to_info=MasterInfo(**b.__dict__), kind=a.kind,
                          changes=ch, n_added=sum(x.kind == "added" for x in ch),
                          n_modified=sum(x.kind == "modified" for x in ch),
                          n_deleted=sum(x.kind == "deleted" for x in ch), warnings=warn)

    # ------------------------------------------------------------ etichette e blocco
    @r.patch("/master/revisions/{n}", response_model=MasterInfo)
    def patch_revision(n: int, body: RevisionPatchIn, c=Depends(ctx(Role.LEAD))):
        """Etichetta e/o blocca una revisione (Lead, Admin). Sbloccare: solo Admin. Una revisione eliminata: 409."""
        if body.label is None and body.locked is None:
            raise HTTPException(422, "Indica almeno un campo: label o locked")
        user, st = c.access.principal.username, c.rt.store
        label = None if body.label is None else " ".join(body.label.split())
        if label is not None and len(label) > LABEL_MAX:
            raise HTTPException(422, f"L'etichetta puo' avere al massimo {LABEL_MAX} caratteri")
        with c.rt.mgr._lock:                                                     # noqa: SLF001
            hist = st.history()
            if not (1 <= n <= len(hist)):
                raise HTTPException(404, f"Revisione {n} inesistente")
            cur = hist[n - 1]
            if cur.purged:
                raise HTTPException(409, f"La revisione {n} non e' piu' conservata dal server: non si puo' "
                                         "etichettare ne' bloccare")
            if body.locked is False and cur.locked and c.access.role < Role.ADMIN:
                raise HTTPException(403, "Solo un Project Administrator puo' sbloccare una revisione")
            try:
                if label is not None and label != cur.label:
                    old = cur.label
                    st.set_label(n, label, user)
                    rec(c, "revision_labeled", f"rev{n}", label=label, previous=old)
                if body.locked is not None and body.locked != cur.locked:
                    st.set_locked(n, body.locked, user)
                    rec(c, "revision_locked" if body.locked else "revision_unlocked", f"rev{n}", label=st.history()[n - 1].label)
            except RevisionPurged as exc:                                        # corsa con la potatura
                raise HTTPException(409, str(exc)) from exc
            return MasterInfo(**st.history()[n - 1].__dict__)

    # ------------------------------------------------------------ lock esclusivo
    @r.get("/lock", response_model=LockOut)
    def lock_state(c=Depends(ctx(Role.VIEWER))):
        return lock_out(locks_for(c.rt, cfg), c.access.principal.username)

    @r.post("/lock", response_model=LockOut)
    def lock_acquire(body: LockIn = LockIn(), c=Depends(ctx(Role.DEVELOPER))):
        """«Prendi in carico»: 409 se e' gia' preso da un altro (con chi, da quando, fino a quando)."""
        user, lm = c.access.principal.username, locks_for(c.rt, cfg)
        with c.rt.mgr._lock:                                                     # noqa: SLF001
            if not c.rt.store.initialized:
                raise HTTPException(409, "Master non inizializzato")
            cur = lm.current()
            if cur is not None and cur.holder != user:
                raise HTTPException(409, _held_msg(cur))
            info, mine = lm.acquire(user, body.note.strip())
            if not mine:
                rec(c, "lock_acquired", "master", expires_at=_dt(info.expires_at).isoformat(), note=info.note)
                announce(c, "lock_changed", f"{user} ha preso in carico il Master")
            return lock_out(lm, user)

    @r.post("/lock/renew", response_model=LockOut)
    def lock_renew(c=Depends(ctx(Role.DEVELOPER))):
        user, lm = c.access.principal.username, locks_for(c.rt, cfg)
        with c.rt.mgr._lock:                                                     # noqa: SLF001
            try:
                lm.renew(user)
            except LockError as exc:
                raise HTTPException(exc.status, exc.message) from exc
            return lock_out(lm, user)

    @r.delete("/lock", response_model=LockOut)
    def lock_release(reason: str = Query(""), c=Depends(ctx(Role.VIEWER))):
        """«Rilascia» (holder) o «Forza rilascio» (solo Admin, con motivo di almeno 5 caratteri)."""
        user, lm = c.access.principal.username, locks_for(c.rt, cfg)
        with c.rt.mgr._lock:                                                     # noqa: SLF001
            cur = lm.current()
            if cur is None:
                return lock_out(lm, user)
            if cur.holder == user:
                lm.release()
                rec(c, "lock_released", "master", held_since=_dt(cur.acquired_at).isoformat())
                announce(c, "lock_changed", f"{user} ha rilasciato il Master")
            elif c.access.role >= Role.ADMIN:
                reason = reason.strip()
                if len(reason) < MIN_LOCK_REASON:
                    raise HTTPException(422, f"Indica il motivo del rilascio forzato (almeno {MIN_LOCK_REASON} caratteri)")
                lm.release()
                rec(c, "lock_forced", "master", holder=cur.holder, held_since=_dt(cur.acquired_at).isoformat(),
                    reason=reason[:300])
                announce(c, "lock_changed", f"{user} ha forzato il rilascio del Master (era in carico a {cur.holder})")
            else:
                raise HTTPException(403, f"{_held_msg(cur)}: solo chi lo ha in carico (o un Admin) puo' rilasciarlo")
            return lock_out(lm, user)

    # ------------------------------------------------------------ check-in dei file binari
    def do_checkin(c, data: bytes, filename: str, message: str) -> MasterInfo:    # noqa: ANN001
        user, st, lm = c.access.principal.username, c.rt.store, locks_for(c.rt, cfg)
        message = (message or "").strip()
        with c.rt.mgr._lock:                                                     # noqa: SLF001
            if not st.initialized:
                raise HTTPException(409, "Master non inizializzato")
            head = st.head()
            if head.kind in MERGEABLE:
                raise HTTPException(422, f"Il Master e' «{KIND_LABELS[head.kind]}»: si aggiorna con «Richiedi Merge». "
                                         "Il check-in diretto e' riservato ai file binari")
            if len(message) < MIN_CHECKIN_MESSAGE:
                raise HTTPException(422, f"Indica il messaggio del check-in (almeno {MIN_CHECKIN_MESSAGE} caratteri)")
            if not data:
                raise HTTPException(422, "Il file e' vuoto")
            cur = lm.current()
            if cur is None:
                raise HTTPException(409, "Per fare il check-in devi prima «Prendere in carico» il Master "
                                         "(la presa in carico e' scaduta o non c'e')")
            if cur.holder != user:
                raise HTTPException(403, f"{_held_msg(cur)}: il check-in lo puo' fare solo chi lo ha in carico")
            ext = canonical_ext(filename, head.kind)
            if ext.lower() != head.ext.lower():
                raise HTTPException(422, f"L'estensione del file ({ext}) non corrisponde a quella del progetto ({head.ext})")
            if hashlib.sha256(data).hexdigest() == head.sha256:
                raise HTTPException(422, "Il file e' identico al Master corrente: nulla da registrare")
            rev = st.commit(data, user, f"Check-in: {message}", None)
            rec(c, "master_checkin", f"rev{rev.revision}", previous_head=head.revision, sha256=rev.sha256,
                size=len(data), filename=(filename or "")[:120], message=message[:200])
            c.rt.mgr.prune_revisions(user)
            lm.release()
            rec(c, "lock_released", "master", via="checkin", held_since=_dt(cur.acquired_at).isoformat())
            announce(c, "master_updated", f"Nuovo Master rev {rev.revision} (check-in di {user})", revision=rev.revision)
            announce(c, "lock_changed", f"{user} ha rilasciato il Master dopo il check-in")
            return c.rt.mgr.master_info()

    @r.post("/master/checkin", response_model=MasterInfo)
    async def checkin(file: UploadFile = File(...), message: str = Form(...), c=Depends(ctx(Role.DEVELOPER))):
        """Nuova revisione di un Master BINARIO (non unibile): serve la presa in carico attiva di chi la invia."""
        data = await file.read()
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        return await run_in_threadpool(do_checkin, c, data, file.filename or "", message)

    return r
