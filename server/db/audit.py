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

from sqlalchemy import select, text

from server.db.base import Database
from server.db.models import AUDIT_TRIGGERS, AuditLog

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

    def purge_project(self, project_id: int) -> int:
        """MANUTENZIONE ESPLICITA: cancella tutto l'audit di un progetto e ricalcola la catena di hash. Ritorna le righe rimosse.

        E' l'unica eccezione all'immutabilita' dell'audit ed e' usata solo dall'eliminazione di un progetto (Admin).
        In una sola transazione e con il lock dell'audit: rimuove i trigger (SQLite), cancella con SQL grezzo le righe del
        progetto, ricalcola ``prev_hash``/``hash`` delle righe rimaste (in ordine di id, cosi' ``verify()`` resta ok) e
        ricrea i trigger identici. Se qualcosa fallisce la transazione torna indietro e l'audit resta com'era.
        Su PostgreSQL non ci sono trigger e si salta quel passaggio.

        Per scelta dell'amministratore l'eliminazione di un progetto non lascia nessuna riga di audit: la catena
        resta valida e di quel progetto non rimane traccia nel registro.
        """
        sqlite = self.db.engine.dialect.name == "sqlite"
        with self._lock, self.db.engine.connect() as cx:
            if sqlite:
                cx.exec_driver_sql("BEGIN IMMEDIATE")        # pysqlite non apre transazioni per il DDL: lo facciamo noi
            try:
                if sqlite:
                    for name in AUDIT_TRIGGERS:
                        cx.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
                removed = cx.execute(text("DELETE FROM audit_log WHERE project_id = :p"), {"p": project_id}).rowcount
                prev = GENESIS
                rows = cx.execute(text("SELECT id, ts, actor, action, project_id, target, details, ip, prev_hash, hash "
                                       "FROM audit_log ORDER BY id")).all()
                for r in rows:
                    h = _digest(prev, r.ts, r.actor, r.action, r.project_id, r.target, r.details, r.ip)
                    if r.prev_hash != prev or r.hash != h:
                        cx.execute(text("UPDATE audit_log SET prev_hash = :ph, hash = :h WHERE id = :i"),
                                   {"ph": prev, "h": h, "i": r.id})
                    prev = h
                if sqlite:
                    for ddl in AUDIT_TRIGGERS.values():
                        cx.exec_driver_sql(ddl)
                cx.commit()
            except BaseException:
                cx.rollback()
                raise
        return max(int(removed or 0), 0)

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

    def for_project(self, project_id: int) -> list[AuditLog]:
        """Tutte le righe di un progetto, dalla piu' vecchia (per l'esportazione)."""
        with self.db.scope() as s:
            rows = list(s.execute(select(AuditLog).where(AuditLog.project_id == project_id)
                                  .order_by(AuditLog.id)).scalars())
            for r in rows:
                s.expunge(r)
            return rows

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
