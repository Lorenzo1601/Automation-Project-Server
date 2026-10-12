"""Persistenza su disco del Master (qualsiasi tipo di file) e dello storico delle revisioni.

Nome ed estensione sono quelli del file importato (``.L5X`` / ``.L5K`` per i Rockwell) e restano gli stessi per tutto il progetto.

Layout (esempio con ``.L5X``)::

    data/
      <NomeFile>.L5X            # copia comoda dell'ultima revisione, con il nome del file importato
      history.json              # metadati di tutte le revisioni
      revisions/rev_000001.L5X  # ogni revisione e' immutabile: serve come BASE per i 3-way merge
      proposals/<id>.L5X        # file inviati dai client
      proposals.json            # audit delle proposte
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common.filekinds import DEFAULT_NAME, L5X, canonical_ext, detect_kind, safe_stem


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@dataclass
class Revision:
    revision: int
    sha256: str
    updated_at: str
    updated_by: str
    message: str = ""
    proposal_id: Optional[str] = None
    kind: str = L5X               # tipo di file (common.filekinds); i progetti creati prima erano tutti L5X
    ext: str = ".L5X"
    name: str = DEFAULT_NAME      # nome del file importato (senza estensione); i progetti piu' vecchi non lo avevano


class MasterStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.rev_dir = self.root / "revisions"
        self.prop_dir = self.root / "proposals"
        for d in (self.root, self.rev_dir, self.prop_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._history: list[Revision] = []
        hp = self.root / "history.json"
        if hp.exists():
            self._history = [Revision(**r) for r in json.loads(hp.read_text("utf-8"))]

    # ------------------------------------------------------------ lettura
    @property
    def initialized(self) -> bool:
        return bool(self._history)

    def head(self) -> Revision:
        with self._lock:
            if not self._history:
                raise LookupError("Master non inizializzato")
            return self._history[-1]

    def history(self) -> list[Revision]:
        with self._lock:
            return list(self._history)

    @property
    def kind(self) -> str:
        return self.head().kind

    @property
    def ext(self) -> str:
        return self.head().ext

    @property
    def name(self) -> str:
        return self.head().name or DEFAULT_NAME

    def _path(self, n: int) -> Optional[Path]:
        with self._lock:
            r = self._history[n - 1] if 1 <= n <= len(self._history) else None
        return self.rev_dir / f"rev_{n:06d}{r.ext}" if r else None

    def read_revision(self, n: Optional[int] = None) -> bytes:
        n = self.head().revision if n is None else n
        p = self._path(n)
        if p is None or not p.exists():
            raise LookupError(f"Revisione {n} inesistente")
        return p.read_bytes()

    def has_revision(self, n: int) -> bool:
        p = self._path(n)
        return p is not None and p.exists()

    # ----------------------------------------------------------- scrittura
    def commit(self, data: bytes, author: str, message: str = "",
               proposal_id: Optional[str] = None, filename: str = "", rename: bool = False) -> Revision:
        """Nuova revisione. La prima fissa tipo ed estensione del Master (da contenuto e nome file); le altre li ereditano.

        ``rename=True`` (reimport del Master) adotta il nome del nuovo file (``safe_stem(filename)``); tipo ed estensione restano.
        """
        with self._lock:
            if self._history:
                kind, ext, name = self._history[-1].kind, self._history[-1].ext, self._history[-1].name or DEFAULT_NAME
                if rename and filename:
                    name = safe_stem(filename)
            else:
                kind = detect_kind(data)
                ext = canonical_ext(filename, kind)
                name = safe_stem(filename)
            n = (self._history[-1].revision + 1) if self._history else 1
            old_copy = (self.root / f"{self._history[-1].name or DEFAULT_NAME}{self._history[-1].ext}") if self._history else None
            _atomic_write(self.rev_dir / f"rev_{n:06d}{ext}", data)
            _atomic_write(self.root / f"{name}{ext}", data)
            if old_copy is not None and old_copy != self.root / f"{name}{ext}":
                old_copy.unlink(missing_ok=True)             # la copia comoda segue il nuovo nome del file
            rev = Revision(n, hashlib.sha256(data).hexdigest(),
                           datetime.now(timezone.utc).isoformat(), author, message, proposal_id, kind, ext, name)
            self._history.append(rev)
            _atomic_write(self.root / "history.json",
                          json.dumps([asdict(r) for r in self._history], indent=2).encode())
            return rev

    def save_upload(self, proposal_id: str, data: bytes) -> Path:
        p = self.prop_dir / f"{proposal_id}{self.ext if self._history else '.L5X'}"
        _atomic_write(p, data)
        return p

    def write_audit(self, records: list[dict]) -> None:
        _atomic_write(self.root / "proposals.json", json.dumps(records, indent=2, default=str).encode())
