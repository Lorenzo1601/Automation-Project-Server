"""File locali del progetto: cartella di lavoro e ricerca dei file candidati al merge (senza dipendenze Qt).

Il Master puo' essere di qualsiasi tipo (L5X / L5K Rockwell, testo, altri IDE): si cercano i file con la sua stessa
estensione, oppure, su richiesta, tutti i file della cartella."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common.filekinds import L5K, L5X, detect_kind

DEFAULT_EXT = ".L5X"
MASTER_COPY = "Master" + DEFAULT_EXT              # copia del Master al momento del checkout (riferimento 'pulito')
_MASTER_VIEW = re.compile(r"^Master_rev\d+\.[^.\\/]+$", re.I)


def master_copy_name(ext: str = DEFAULT_EXT) -> str:
    return "Master" + (ext or DEFAULT_EXT)


def norm_ext(ext: "str | None") -> str:
    ext = (ext or "").strip()
    return DEFAULT_EXT if not ext else (ext if ext.startswith(".") else "." + ext)
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("_") or "x"


def default_workdir(workspace: "str | Path", username: str, project_slug: str) -> Path:
    """Cartella proposta se l'utente non ne ha scelta una: ``<workspace>/<utente>/<progetto>``."""
    return Path(workspace) / slug(username) / slug(project_slug)


@dataclass
class Candidate:
    path: Path
    size: int
    mtime: float
    is_working: bool = False          # e' il file creato dall'ultimo checkout
    modified: bool = False            # modificato dopo il checkout (piu' recente della copia Master.L5X)
    unchanged: bool = False           # contenuto identico al Master del checkout: niente da unire
    looks_ok: bool = True             # il contenuto e' coerente con il tipo del Master (sempre vero per file generici)

    @property
    def looks_l5x(self) -> bool:      # nome storico
        return self.looks_ok

    @property
    def name(self) -> str:
        return self.path.name


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _head(path: Path, n: int = 8192) -> Optional[bytes]:
    try:
        with path.open("rb") as f:
            return f.read(n)
    except OSError:
        return None


def looks_like_l5x(path: Path) -> bool:
    head = _head(path, 4096)
    return head is not None and b"RSLogix5000Content" in head


def looks_like_kind(path: Path, kind: str) -> bool:
    """Il file e' del tipo atteso? Per L5X / L5K si controlla il contenuto; per ogni altro tipo vale qualsiasi file."""
    if kind not in (L5X, L5K):
        return _head(path, 1) is not None
    head = _head(path)
    return head is not None and detect_kind(head) == kind


def find_merge_candidates(folder: "str | Path", working_file: str = "", max_depth: int = 2, ext: str = DEFAULT_EXT,
                          kind: str = L5X, all_files: bool = False) -> list[Candidate]:
    """Elenca i file della cartella (e sottocartelle fino a ``max_depth``) che possono essere inviati al merge.

    Di norma sono i file con la stessa estensione del Master (``ext``: ``.L5X``, ``.L5K``, ...); con ``all_files`` si
    elencano tutti i file. Esclude la copia ``Master<ext>`` e i ``Master_revN.*`` scaricati (non sono lavoro
    dell'utente) e i file temporanei. Ordine: piu' recenti per primi. ``modified``/``unchanged`` si calcolano rispetto
    alla copia del Master.
    """
    root = Path(folder)
    ext = norm_ext(ext)
    if not root.is_dir():
        return []
    master_name = master_copy_name(ext)
    master = root / master_name
    m_mtime = master.stat().st_mtime if master.is_file() else None
    m_size = master.stat().st_size if master.is_file() else None
    wf = Path(working_file).resolve() if working_file else None
    out: list[Candidate] = []

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.is_dir():
                if depth < max_depth and p.name not in _SKIP_DIRS and not p.name.startswith("."):
                    walk(p, depth + 1)
                continue
            n = p.name
            if (not all_files and p.suffix.lower() != ext.lower()) or n.startswith(("~", ".")) \
                    or n.lower().endswith((".tmp", ".bak", ".lock")) or n.lower() == master_name.lower() \
                    or n.lower() == MASTER_COPY.lower() or _MASTER_VIEW.match(n):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append(Candidate(p, st.st_size, st.st_mtime, is_working=bool(wf and p.resolve() == wf)))
    walk(root, 0)

    cache: dict = {}
    for c in out:
        c.looks_ok = looks_like_kind(c.path, kind)
        if m_mtime is not None:
            c.modified = c.mtime > m_mtime + 2.0
            if c.size == m_size:                                     # l'hash costa: solo se le dimensioni coincidono
                if "m" not in cache:
                    cache["m"] = _sha(master)
                c.unchanged = _sha(c.path) == cache["m"]
                if c.unchanged:
                    c.modified = False
        else:
            c.modified = True
    out.sort(key=lambda c: c.mtime, reverse=True)
    return out


def pick_default(cands: list[Candidate]) -> Optional[Candidate]:
    """Il candidato piu' probabile: il file piu' recente realmente modificato, altrimenti il piu' recente."""
    for c in cands:
        if c.modified and c.looks_ok:
            return c
    return cands[0] if cands else None
