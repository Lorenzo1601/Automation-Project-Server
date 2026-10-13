"""Persistenza su disco del Master (qualsiasi tipo di file) e dello storico delle revisioni.

Nome ed estensione sono quelli del file importato (``.L5X`` / ``.L5K`` per i Rockwell) e restano gli stessi per tutto il progetto.

Layout (esempio con ``.L5X``)::

    data/
      <NomeFile>.L5X            # copia comoda dell'ultima revisione, con il nome del file importato
      history.json              # metadati di tutte le revisioni (anche di quelle eliminate dal limite: ``purged``)
      revisions/rev_000001.L5X  # ogni revisione e' immutabile: serve come BASE per i 3-way merge;
                                # il server ne tiene solo le ultime ``max_revisions`` (``MasterStore.prune``)
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


class RevisionPurged(LookupError):
    """Il file di questa revisione e' stato eliminato dal server per rispettare il limite di revisioni conservate."""


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
    purged: bool = False          # file eliminato dal server (limite di revisioni): restano solo i metadati
    label: str = ""               # etichetta libera (es. «FAT», «Consegna cliente»), max 80 caratteri
    locked: bool = False          # bloccata: la potatura (limite di revisioni) non la elimina mai
    labeled_by: str = ""
    locked_by: str = ""


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

    def is_purged(self, n: int) -> bool:
        with self._lock:
            return 1 <= n <= len(self._history) and self._history[n - 1].purged

    def read_revision(self, n: Optional[int] = None) -> bytes:
        n = self.head().revision if n is None else n
        if self.is_purged(n):
            raise RevisionPurged(f"La revisione {n} non e' piu' conservata dal server (limite di revisioni raggiunto)")
        p = self._path(n)
        if p is None or not p.exists():
            raise LookupError(f"Revisione {n} inesistente")
        return p.read_bytes()

    def has_revision(self, n: int) -> bool:
        """True se la revisione esiste e il suo file e' ancora sul disco (False anche per quelle eliminate)."""
        p = None if self.is_purged(n) else self._path(n)
        return p is not None and p.exists()

    def locked_count(self) -> int:
        """Revisioni bloccate (non eliminabili dal limite di revisioni)."""
        with self._lock:
            return sum(1 for r in self._history if r.locked and not r.purged)

    def purged_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._history if r.purged)

    def kept_revisions(self) -> list[Revision]:
        with self._lock:
            return [r for r in self._history if not r.purged]

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
            self._write_history()
            return rev

    def _write_history(self) -> None:
        _atomic_write(self.root / "history.json", json.dumps([asdict(r) for r in self._history], indent=2).encode())

    def set_label(self, n: int, label: str, by: str) -> Revision:
        """Imposta (o toglie, con ``""``) l'etichetta di una revisione conservata. ``RevisionPurged`` se il file non c'e' piu'."""
        with self._lock:
            r = self._rev_for_edit(n)
            r.label, r.labeled_by = label, by if label else ""
            self._write_history()
            return r

    def set_locked(self, n: int, locked: bool, by: str) -> Revision:
        """Blocca/sblocca una revisione conservata: una bloccata non viene mai potata dal limite di revisioni."""
        with self._lock:
            r = self._rev_for_edit(n)
            r.locked, r.locked_by = locked, by if locked else ""
            self._write_history()
            return r

    def _rev_for_edit(self, n: int) -> Revision:
        if not (1 <= n <= len(self._history)):
            raise LookupError(f"Revisione {n} inesistente")
        r = self._history[n - 1]
        if r.purged:
            raise RevisionPurged(f"La revisione {n} non e' piu' conservata dal server: non si puo' piu' etichettare o bloccare")
        return r

    def prune(self, keep: int, protect: Optional[set[int]] = None) -> list[int]:
        """Elimina dal disco i file delle revisioni vecchie e le marca ``purged`` (i metadati restano nello storico).

        Restano: le ultime ``keep`` revisioni NON bloccate (la testa inclusa, mai potata), tutte le revisioni BLOCCATE
        (non contano nel limite: si sommano ad esso) e quelle in ``protect``.
        Ritorna i numeri delle revisioni appena eliminate.
        """
        protect = protect or set()
        with self._lock:
            if not self._history:
                return []
            head = self._history[-1].revision
            gone: list[int] = []
            recent = {r.revision for r in reversed(self._history) if not r.purged and not r.locked}
            recent = set(sorted(recent, reverse=True)[:max(keep, 1)])
            for r in self._history:
                if r.purged or r.locked or r.revision == head or r.revision in recent or r.revision in protect:
                    continue
                (self.rev_dir / f"rev_{r.revision:06d}{r.ext}").unlink(missing_ok=True)
                r.purged = True
                gone.append(r.revision)
            if gone:
                self._write_history()
            return gone

    def save_upload(self, proposal_id: str, data: bytes) -> Path:
        p = self.prop_dir / f"{proposal_id}{self.ext if self._history else '.L5X'}"
        _atomic_write(p, data)
        return p

    def write_audit(self, records: list[dict]) -> None:
        _atomic_write(self.root / "proposals.json", json.dumps(records, indent=2, default=str).encode())
