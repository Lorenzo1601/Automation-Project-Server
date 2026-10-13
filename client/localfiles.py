"""File locali del progetto: cartella di lavoro e ricerca dei file candidati al merge (senza dipendenze Qt).

Il Master puo' essere di qualsiasi tipo (L5X / L5K Rockwell, testo, altri IDE): si cercano i file con la sua stessa
estensione, oppure, su richiesta, tutti i file della cartella."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from common.filekinds import DEFAULT_NAME, L5K, L5X, PLCOPEN, detect_kind, rev_filename

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


BRANCHES_DIR = "branches"      # sotto la cartella del progetto: <cartella>/branches/<slug>/ (ognuna con il proprio .aps)


def branch_dirname(branch_slug: str) -> str:
    """Nome sicuro (una sola componente di percorso) della cartella di un branch."""
    return re.sub(r"[^\w.-]+", "_", str(branch_slug or "")).strip("._") or "branch"


def branch_folder(folder: "str | Path", branch_slug: "str | None") -> Path:
    """Cartella locale di un branch: ``<cartella del progetto>/branches/<slug>``; senza branch (principale) e' la
    cartella del progetto stessa, quindi il layout del principale resta invariato."""
    if not branch_slug:
        return Path(folder)
    return Path(folder) / BRANCHES_DIR / branch_dirname(branch_slug)


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
    """Il file e' del tipo atteso? Per L5X / L5K / PLCopen si controlla il contenuto; per ogni altro tipo vale qualsiasi file."""
    if kind not in (L5X, L5K, PLCOPEN):
        return _head(path, 1) is not None
    head = _head(path)
    return head is not None and detect_kind(head) == kind


REF_DIR = ".aps"          # copie 'pulite' dei Master scaricati (cartella nascosta): riferimento per capire cosa hai modificato


def ref_path(folder: "str | Path", name: str, revision: int, ext: str) -> Path:
    """Dove sta la copia pulita della revisione ``revision``: ``<cartella>/.aps/<nome>_revN<ext>``."""
    return Path(folder) / REF_DIR / rev_filename(name, revision, norm_ext(ext))


def hide_ref_dir(folder: "str | Path") -> None:
    """Su Windows nasconde la cartella ``.aps`` (altrove il punto iniziale basta)."""
    d = Path(folder) / REF_DIR
    try:
        if os.name == "nt" and d.is_dir():
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(d), 0x2)          # FILE_ATTRIBUTE_HIDDEN
    except Exception:                                                        # noqa: BLE001 - solo estetica
        pass


def find_merge_candidates(folder: "str | Path", working_file: str = "", max_depth: int = 2, ext: str = DEFAULT_EXT,
                          kind: str = L5X, all_files: bool = False, name: str = "", base_rev: int = 0) -> list[Candidate]:
    """Elenca i file della cartella (e sottocartelle fino a ``max_depth``) che possono essere inviati al merge.

    Di norma sono i file con la stessa estensione del Master (``ext``: ``.L5X``, ``.L5K``, ...); con ``all_files`` si
    elencano tutti i file. Non elenca i file temporanei ne' le copie del Master scaricate e mai toccate
    (``<name>_revN<ext>`` identiche alla copia pulita in ``.aps``), salvo il file del checkout. Ordine: piu' recenti
    per primi. ``modified``/``unchanged`` si calcolano rispetto alla copia pulita della revisione del file (o, se non
    c'e', della revisione base ``base_rev``).

    Senza ``name`` (progetti creati con le versioni precedenti) il riferimento e' la vecchia ``Master<ext>`` e le
    ``Master_revN`` sono escluse.
    """
    root = Path(folder)
    ext = norm_ext(ext)
    if not root.is_dir():
        return []
    legacy_name = master_copy_name(ext)
    legacy = root / legacy_name
    view_re = re.compile(rf"^{re.escape(name)}_rev(\d+){re.escape(ext)}$", re.I) if name else None
    wf = Path(working_file).resolve() if working_file else None
    out: list[Candidate] = []
    refs: dict[int, Optional[Path]] = {}

    def ref_for(rev: int) -> Optional[Path]:
        if rev not in refs:
            p = ref_path(root, name, rev, ext) if name and rev > 0 else None
            refs[rev] = p if p is not None and p.is_file() else None
        return refs[rev]

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.is_dir():
                if depth == 0 and p.name == BRANCHES_DIR:
                    continue                                        # i file dei branch non sono candidati al merge del principale
                if depth < max_depth and p.name not in _SKIP_DIRS and not p.name.startswith("."):
                    walk(p, depth + 1)
                continue
            n = p.name
            if (not all_files and p.suffix.lower() != ext.lower()) or n.startswith(("~", ".")) \
                    or n.lower().endswith((".tmp", ".bak", ".lock")):
                continue
            if not name and (n.lower() in (legacy_name.lower(), MASTER_COPY.lower()) or _MASTER_VIEW.match(n)):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append(Candidate(p, st.st_size, st.st_mtime, is_working=bool(wf and p.resolve() == wf)))
    walk(root, 0)

    cache: dict = {}

    def sha_of(path: Path) -> str:
        k = str(path)
        if k not in cache:
            cache[k] = _sha(path)
        return cache[k]

    kept: list[Candidate] = []
    for c in out:
        c.looks_ok = looks_like_kind(c.path, kind)
        ref: Optional[Path] = None
        if view_re is not None:
            m = view_re.match(c.name)
            ref = ref_for(int(m.group(1))) if m else None
            if ref is None:
                ref = ref_for(base_rev)
        elif legacy.is_file():
            ref = legacy
        if ref is None:
            c.modified = True
        else:
            r = ref.stat()
            c.modified = c.mtime > r.st_mtime + 2.0
            if c.size == r.st_size:                                  # l'hash costa: solo se le dimensioni coincidono
                c.unchanged = sha_of(c.path) == sha_of(ref)
                if c.unchanged:
                    c.modified = False
        if view_re is not None and view_re.match(c.name) and c.unchanged and not c.is_working:
            continue                                                 # copia del Master scaricata e mai toccata
        kept.append(c)
    kept.sort(key=lambda c: c.mtime, reverse=True)
    return kept


def pick_default(cands: list[Candidate]) -> Optional[Candidate]:
    """Il candidato piu' probabile: il file piu' recente realmente modificato, altrimenti il piu' recente."""
    for c in cands:
        if c.modified and c.looks_ok:
            return c
    return cands[0] if cands else None


# ------------------------------------------------------------------ pulizia delle vecchie revisioni sul PC
def _same_bytes(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and _sha(a) == _sha(b)
    except OSError:
        return False


def _revisions_in(directory: Path, pattern: "re.Pattern[str]") -> dict[int, Path]:
    """File regolari (mai collegamenti simbolici) di ``directory`` il cui nome rispetta ESATTAMENTE il pattern."""
    out: dict[int, Path] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return out
    for p in entries:
        m = pattern.fullmatch(p.name)
        if m is None:
            continue
        try:
            if p.is_symlink() or not p.is_file():
                continue
        except OSError:
            continue
        out[int(m.group(1))] = p
    return out


def prune_local(folder: "str | Path", name: str, ext: str, keep: int, protect_paths: "Iterable[str | Path]" = (),
                known_sha: Optional["Mapping[int, str]"] = None) -> list[Path]:
    """Tiene sul PC solo le ``keep`` revisioni piu' recenti del Master scaricate in ``folder``.

    Considera i file ``<name>_revN<ext>`` direttamente in ``folder`` e le copie di riferimento omonime in ``.aps``;
    le revisioni oltre le ``keep`` piu' alte (per numero) vengono rimosse. Ritorna i file cancellati.

    REGOLE DI SICUREZZA (nel dubbio non si cancella nulla):
      * mai un file (ne' la copia ``.aps``) di una revisione che compare in ``protect_paths`` (file di lavoro corrente,
        sua base): basta che un percorso protetto abbia il nome ``<name>_revN<ext>`` per proteggere la revisione N;
      * un file visibile si cancella solo se e' IDENTICO byte per byte alla propria copia pulita in ``.aps`` (o, in
        assenza della copia, all'hash noto in ``known_sha[N]``): se e' diverso potrebbe contenere lavoro dell'utente,
        se non si puo' verificare non si tocca; in entrambi i casi resta anche la copia di riferimento;
      * solo nomi che rispettano esattamente ``<name>_rev<numero><ext>`` (numero senza zeri iniziali), solo file
        regolari, mai collegamenti simbolici, mai nulla fuori da ``folder`` / ``folder/.aps``;
      * ``keep`` < 1 o cartella inesistente: non fa nulla; ogni errore di I/O lascia il file dov'e'.
    """
    removed: list[Path] = []
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        return removed
    root = Path(folder)
    if keep < 1 or not root.is_dir():
        return removed
    try:
        root_r = root.resolve()
        ref_dir = root / REF_DIR
        ref_dir_r = ref_dir.resolve() if ref_dir.is_dir() else None
    except OSError:
        return removed
    ext = norm_ext(ext)
    pat = re.compile(rf"{re.escape(name or DEFAULT_NAME)}_rev([1-9][0-9]*){re.escape(ext)}")

    protected_revs: set[int] = set()
    protected_paths: set[Path] = set()
    for p in protect_paths or ():
        if not p:
            continue
        pp = Path(p)
        try:
            protected_paths.add(pp.resolve())
        except OSError:
            pass
        m = pat.fullmatch(pp.name)
        if m:
            protected_revs.add(int(m.group(1)))

    visible = _revisions_in(root, pat)
    refs = _revisions_in(ref_dir, pat) if ref_dir_r is not None else {}
    revs = sorted(set(visible) | set(refs), reverse=True)

    def inside(p: Path, parent: Path) -> bool:
        try:
            return p.resolve().parent == parent
        except OSError:
            return False

    def remove(p: Path) -> bool:
        try:
            if p.resolve() in protected_paths:
                return False
            p.unlink()
        except OSError:
            return False
        removed.append(p)
        return True

    for rev in revs[keep:]:
        if rev in protected_revs:
            continue
        v, r = visible.get(rev), refs.get(rev)
        if r is not None and ref_dir_r is not None and not inside(r, ref_dir_r):
            r = None
        if v is not None:
            if not inside(v, root_r):
                continue
            if r is not None:
                clean = _same_bytes(v, r)
            elif known_sha and rev in known_sha:
                try:
                    clean = _sha(v) == known_sha[rev]
                except OSError:
                    clean = False
            else:
                clean = False                                      # non verificabile -> non si cancella
            if not clean or not remove(v):
                continue                                           # modificato / protetto / errore: resta tutto
        if r is not None:
            remove(r)
    return removed
