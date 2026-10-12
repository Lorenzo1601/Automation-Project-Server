"""Audit trail non modificabile: registro append-only con catena di hash (tamper-evident).

Ogni riga contiene ``prev_hash`` (hash della riga precedente) e ``hash = SHA256(prev_hash | campi)``.
Modificare o cancellare una riga (anche direttamente sul DB) rompe la catena: ``verify()`` la rileva.
Su SQLite UPDATE/DELETE sono inoltre bloccati da trigger.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from server.db.base import Database
from server.db.models import AuditLog

GENESIS = "0" * 64


def _digest(prev: str, ts: str, actor: str, action: str, project_id: Optional[int], target: str,
            details: str, ip: str) -> str:
    raw = "\x1f".join([prev, ts, actor, action, "" if project_id is None else str(project_id), target, details, ip])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuditService:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()

    def record(self, action: str, actor: str, *, project_id: Optional[int] = None, target: str = "",
               details: Optional[dict[str, Any]] = None, ip: str = "") -> AuditLog:
        det = json.dumps(details or {}, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock, self.db.scope() as s:
            last = s.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)).scalar()
            prev = last or GENESIS
            ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            row = AuditLog(ts=ts, actor=actor, action=action, project_id=project_id, target=target[:200],
                           details=det, ip=ip, prev_hash=prev,
                           hash=_digest(prev, ts, actor, action, project_id, target[:200], det, ip))
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def verify(self) -> tuple[bool, Optional[int], int]:
        """-> (integra, id della prima riga incoerente, righe controllate)."""
        prev, n = GENESIS, 0
        with self.db.scope() as s:
            for r in s.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
                n += 1
                if r.prev_hash != prev or r.hash != _digest(prev, r.ts, r.actor, r.action, r.project_id,
                                                           r.target, r.details, r.ip):
                    return False, r.id, n
                prev = r.hash
        return True, None, n

    def query(self, *, project_ids: Optional[set[int]] = None, include_global: bool = True,
              action: Optional[str] = None, actor: Optional[str] = None, limit: int = 200,
              offset: int = 0) -> list[AuditLog]:
        """Filtra per progetti visibili. ``project_ids=None`` = nessun filtro (Admin)."""
        with self.db.scope() as s:
            q = select(AuditLog).order_by(AuditLog.id.desc())
            if project_ids is not None:
                cond = AuditLog.project_id.in_(project_ids or {-1})
                if include_global:
                    cond = cond | AuditLog.project_id.is_(None)
                q = q.where(cond)
            if action:
                q = q.where(AuditLog.action == action)
            if actor:
                q = q.where(AuditLog.actor == actor)
            rows = list(s.execute(q.limit(min(limit, 1000)).offset(offset)).scalars())
            for r in rows:
                s.expunge(r)
            return rows
