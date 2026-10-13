"""Lock esclusivo (presa in carico) del Master di un progetto.

Un solo lock per progetto, salvato in ``<cartella progetto>/lock.json`` (sopravvive al riavvio del server e sparisce
con il progetto). Scade da solo: nessun job, il controllo e' *pigro* a ogni lettura (``current``). Il clock e' iniettabile
(test). Nessuna dipendenza da FastAPI: la logica dei permessi e l'audit stanno in ``server/revisions_ext.py``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Union


class LockError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class LockInfo:
    holder: str
    acquired_at: float            # epoch (s)
    expires_at: float
    note: str = ""


class LockManager:
    def __init__(self, path: Path, ttl_seconds: Union[float, Callable[[], float]] = 8 * 3600,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._ttl = ttl_seconds
        self.clock = clock
        self._mutex = threading.RLock()
        self._info: Optional[LockInfo] = None
        try:
            self._info = LockInfo(**json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            self._info = None

    # ------------------------------------------------------------------ interni
    @property
    def ttl(self) -> float:
        return float(self._ttl() if callable(self._ttl) else self._ttl)

    def _save(self) -> None:
        if self._info is None:
            self.path.unlink(missing_ok=True)
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self._info)), "utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ lettura
    def current(self) -> Optional[LockInfo]:
        """Lock attivo oppure ``None``: se e' scaduto viene eliminato adesso (controllo pigro)."""
        with self._mutex:
            if self._info is not None and self._info.expires_at <= self.clock():
                self._info = None
                self._save()
            return self._info

    # ----------------------------------------------------------------- scrittura
    def acquire(self, user: str, note: str = "") -> tuple[LockInfo, bool]:
        """Prende in carico (o rinnova, se l'holder e' gia' ``user``). Ritorna (lock, True se era gia' suo)."""
        with self._mutex:
            cur = self.current()
            now = self.clock()
            if cur is not None and cur.holder != user:
                raise LockError(409, f"Il Master e' gia' in carico a {cur.holder}")
            mine = cur is not None
            self._info = LockInfo(user, cur.acquired_at if cur else now, now + self.ttl,
                                  note if note or not cur else cur.note)
            self._save()
            return self._info, mine

    def renew(self, user: str) -> LockInfo:
        with self._mutex:
            cur = self.current()
            if cur is None:
                raise LockError(409, "Nessuna presa in carico attiva (scaduta o rilasciata): prendi di nuovo in carico")
            if cur.holder != user:
                raise LockError(403, f"Il Master e' in carico a {cur.holder}: solo chi lo ha in carico puo' rinnovare")
            cur.expires_at = self.clock() + self.ttl
            self._save()
            return cur

    def release(self) -> Optional[LockInfo]:
        """Toglie il lock (i permessi sono verificati dal chiamante). Ritorna quello rilasciato, se c'era."""
        with self._mutex:
            cur = self.current()
            self._info = None
            self._save()
            return cur
