"""Branch di progetto: linee alternative del Master con revisioni, lock, richieste di merge e commenti propri.

Un branch e' un *runtime separato* (``MasterStore`` + ``SessionManager`` + ``lock.json``) nella cartella
``data/projects/<slug>/branches/<branch>/``; i metadati stanno nella tabella ``branches`` (``server/db/models_branches.py``).
Il principale e' il runtime normale del progetto. Tutti gli endpoint ``/api/projects/{pid}/...`` accettano ``?branch=<slug>``
(vedi ``ctx`` in ``server/main.py``); qui ci sono la gestione dei branch e le operazioni che attraversano i due runtime.

* ``team``      creato da Lead/Admin; si lavora con le normali richieste di merge e il consenso, come sul principale.
* ``personal``  creato da Developer (o piu' alto), al massimo ``MAX_PERSONAL_BRANCHES`` aperti per utente e progetto;
                scrive solo il proprietario (o un Admin). Salvataggi diretti (``/commit``) e consenso del solo autore.

Operazioni (endpoint sotto ``/api/projects/{pid}/branches``):
  GET    ""                 elenco (Viewer+)               POST   ""             crea (Developer+, team: Lead+)
  GET    "/{slug}"          dettaglio (Viewer+)            POST   "/{slug}/commit"  salvataggio diretto (personale)
  POST   "/{slug}/sync"     «Aggiorna dal principale» = richiesta di merge NEL branch
  POST   "/{slug}/merge"    «Unisci nel principale»   = richiesta di merge NEL principale (consenso normale)
  POST   "/{slug}/close"    chiude (sola lettura)          DELETE "/{slug}"      elimina il branch e i suoi file

Direzione del sync: la base e' ``principale[base_main_revision]``; «mine» = testa del branch, «theirs» = testa del principale
(``changes`` = modifiche del branch, ``upstream_changes`` = novita' del principale). Il risultato diventa una nuova revisione
del branch e ``base_main_revision`` avanza alla revisione del principale che era la testa quando e' stato chiesto il sync.
Il merge e' una normale richiesta nel principale con base ``principale[base_main_revision]`` e file = testa del branch;
consolidata la richiesta il branch passa a ``merged``. Le revisioni del principale usate come base dai branch aperti non
vengono potate dal limite di revisioni.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select

from common.roles import Role
from common.schemas import ACTIVE_STATES, Event, MasterInfo, PreviewOut, ProposalDetail
from common.schemas_branches import MAX_PERSONAL_BRANCHES, BranchCreate, BranchOut
from server.config import Settings
from server.db.audit import AuditService
from server.db.models import Project
from server.db.models_branches import Branch
from server.session_manager import Proposal, SessionError

log = logging.getLogger("aps.branches")

RESERVED_SLUGS = {"main", "master", "principale"}
DEFAULT_COMMIT_MESSAGE = "Salvataggio nel branch"


@dataclass(frozen=True)
class BranchInfo:
    """Istantanea immutabile di una riga della tabella ``branches``."""
    id: int
    project_id: int
    name: str
    slug: str
    kind: str
    owner: str
    parent: str
    base_main_revision: int
    sync_revision: int
    state: str
    merged_main_revision: Optional[int]
    created_at: datetime


def _info(r: Branch) -> BranchInfo:
    ts = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
    return BranchInfo(r.id, r.project_id, r.name, r.slug, r.kind, r.owner, r.parent or "", r.base_main_revision,
                      r.sync_revision or 1, r.state, r.merged_main_revision, ts)


def slugify_branch(name: str) -> str:
    """Nome -> slug: minuscolo senza accenti, solo ``a-z 0-9 - _`` (max 40 caratteri), mai vuoto."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-_")[:40].strip("-_")
    return s or "branch"


def can_write(info: BranchInfo, role: Role, username: str) -> bool:
    """Chi puo' scrivere: Developer+ sui branch team, proprietario o Admin su quelli personali; mai se non e' aperto."""
    if info.state != "open" or role < Role.DEVELOPER:
        return False
    return role >= Role.ADMIN or info.kind == "team" or info.owner == username


def can_delete(info: BranchInfo, role: Role, username: str) -> bool:
    """Chiudere/eliminare: proprietario, Admin e (solo per i team) i Lead."""
    if role < Role.DEVELOPER:
        return False
    return role >= Role.ADMIN or info.owner == username or (info.kind == "team" and role >= Role.LEAD)


class BranchService:
    """Metadati dei branch nel database + regole che toccano il registry (protezione delle basi, ganci di consolidamento)."""

    def __init__(self, reg) -> None:                                              # noqa: ANN001  (ProjectRegistry)
        self.reg = reg

    # ------------------------------------------------------------------ database
    def get(self, project_id: int, slug: str) -> Optional[BranchInfo]:
        with self.reg.db.scope() as s:
            r = s.scalar(select(Branch).where(Branch.project_id == project_id, Branch.slug == slug))
            return _info(r) if r else None

    def rows(self, project_id: int, states: Optional[tuple[str, ...]] = None) -> list[BranchInfo]:
        with self.reg.db.scope() as s:
            q = select(Branch).where(Branch.project_id == project_id).order_by(Branch.id)
            if states:
                q = q.where(Branch.state.in_(states))
            return [_info(r) for r in s.scalars(q)]

    def count_open_personal(self, project_id: int, owner: str) -> int:
        with self.reg.db.scope() as s:
            return s.scalar(select(func.count()).select_from(Branch).where(
                Branch.project_id == project_id, Branch.kind == "personal", Branch.owner == owner,
                Branch.state == "open")) or 0

    @staticmethod
    def delete_rows(session, project_id: int) -> int:                            # noqa: ANN001
        """Cancella i branch di un progetto nella transazione di chi chiama (eliminazione del progetto)."""
        return session.query(Branch).filter_by(project_id=project_id).delete()

    def _update(self, project_id: int, slug: str, **fields) -> Optional[BranchInfo]:
        with self.reg.db.scope() as s:
            r = s.scalar(select(Branch).where(Branch.project_id == project_id, Branch.slug == slug))
            if r is None:
                return None
            for k, v in fields.items():
                setattr(r, k, v)
            s.flush()
            return _info(r)

    # ------------------------------------------------------------------ protezione delle basi
    def protected_main_revisions(self, project_id: int) -> set[int]:
        """Revisioni del principale da non potare: basi dei branch aperti + testa portata dai sync ancora in corso."""
        out = {b.base_main_revision for b in self.rows(project_id, ("open",))}
        for rt in self.reg.branch_runtimes(project_id):
            try:
                props = list(rt.mgr._proposals.values())                          # noqa: SLF001  (copia atomica, senza lock)
            except RuntimeError:                                                  # pragma: no cover
                continue
            out |= {p.meta["main_head"] for p in props
                    if p.state in ACTIVE_STATES and p.meta.get("kind") == "sync" and "main_head" in p.meta}
        return out

    # ------------------------------------------------------------------ eventi / audit
    def announce(self, project_id: int, typ: str, info: BranchInfo, message: str, actor: str = "",
                 revision: Optional[int] = None) -> None:
        """Evento di ciclo di vita del branch: va a TUTTI i client del progetto (principale e branch)."""
        ev = Event(type=typ, message=message, developer=actor or None, branch=info.slug, revision=revision)
        self.reg.notifier.publish(ev.model_dump(mode="json"), None, scope=f"{project_id}:")

    def record(self, project_id: int, action: str, actor: str, info: BranchInfo, ip: str = "", **details) -> None:
        self.reg.audit.record(action, actor, project_id=project_id, target=info.slug, ip=ip,
                              details={"name": info.name, "slug": info.slug, "kind": info.kind, "owner": info.owner,
                                       **details})

    def cancel_main_merges(self, project_id: int, slug: str, reason: str, actor: str = "system") -> list[str]:
        """Annulla le richieste di unione di questo branch ancora aperte nel principale."""
        try:
            main = self.reg.runtime(project_id)
        except LookupError:
            return []
        return main.mgr.cancel_matching(lambda q: q.meta.get("source_branch") == slug, reason, actor)

    # ------------------------------------------------------------------ ganci del SessionManager
    def main_finalize_guard(self, project_id: int, p: Proposal) -> Optional[str]:
        """Prima di consolidare nel principale una richiesta nata da un branch: il branch deve essere ancora aperto e immutato."""
        slug = p.meta.get("source_branch")
        if not slug:
            return None
        info = self.get(project_id, slug)
        if info is None:
            return f"il branch «{slug}» non esiste piu'"
        if info.state != "open":
            return f"il branch «{info.name}» non e' piu' aperto"
        try:
            head = self.reg.runtime(project_id, slug).store.head().revision
        except LookupError:
            return f"il branch «{info.name}» non e' piu' disponibile"
        if head != p.meta.get("branch_head"):
            return f"il branch «{info.name}» e' cambiato dopo la richiesta: rifai «Unisci nel principale»"
        return None

    def on_main_merged(self, project_id: int, p: Proposal, rev) -> None:         # noqa: ANN001
        """Una richiesta e' stata consolidata nel principale: se veniva da un branch, il branch passa a ``merged``."""
        slug = p.meta.get("source_branch")
        if not slug:
            return
        info = self._update(project_id, slug, state="merged", merged_main_revision=rev.revision)
        if info is None:
            return
        self.record(project_id, "branch_merged", p.author, info, revision=rev.revision,
                    branch_revision=p.meta.get("branch_head"), proposal=p.id)
        self.cancel_main_merges(project_id, slug, f"il branch «{info.name}» e' gia' stato unito")
        self.announce(project_id, "branch_updated", info, f"Il branch «{info.name}» e' stato unito nel principale "
                      f"(rev {rev.revision})", p.author, rev.revision)

    def branch_finalize_guard(self, project_id: int, slug: str, p: Proposal) -> Optional[str]:
        info = self.get(project_id, slug)
        if info is None or info.state != "open":
            return "il branch non e' piu' aperto"
        return None

    def on_branch_merged(self, project_id: int, slug: str, p: Proposal, rev) -> None:   # noqa: ANN001
        """Una richiesta e' stata consolidata NEL branch (sync o richiesta normale): aggiorna i metadati e avvisa."""
        info = self.get(project_id, slug)
        if info is None:
            return
        if p.meta.get("kind") == "sync":
            info = self._update(project_id, slug, base_main_revision=p.meta["main_head"], sync_revision=rev.revision) or info
            self.record(project_id, "branch_synced", p.author, info, revision=rev.revision,
                        main_revision=p.meta["main_head"], proposal=p.id)
        self.cancel_main_merges(project_id, slug, f"il branch «{info.name}» e' cambiato")
        self.announce(project_id, "branch_updated", info, f"Il branch «{info.name}» e' alla rev {rev.revision}",
                      p.author, rev.revision)

    # ------------------------------------------------------------------ DTO
    def out(self, info: BranchInfo, role: Role, username: str, archived: bool = False) -> BranchOut:
        pid = info.project_id
        try:
            brt = self.reg.runtime(pid, info.slug)
            head = brt.store.head().revision if brt.store.initialized else 0
            purged = brt.store.purged_count()
        except LookupError:
            head, purged = 0, 0
        behind = ahead = 0
        if info.state == "open":
            main = self.reg.runtime(pid)
            if main.store.initialized:
                behind = max(0, main.store.head().revision - info.base_main_revision)
            ahead = max(0, head - info.sync_revision)
        return BranchOut(
            id=info.id, name=info.name, slug=info.slug, kind=info.kind, owner=info.owner, created_at=info.created_at,
            state=info.state, base_main_revision=info.base_main_revision, head_revision=head, behind=behind, ahead=ahead,
            purged_revisions=purged, merged_main_revision=info.merged_main_revision,
            can_write=not archived and can_write(info, role, username), can_delete=can_delete(info, role, username))


def check_write(info: Optional[BranchInfo], role: Role, username: str) -> BranchInfo:
    """Per le richieste che scrivono (ruolo Developer+) su un branch: 404 / 409 (non aperto) / 403 (personale altrui)."""
    if info is None:
        raise HTTPException(404, "Branch inesistente")
    if info.state != "open":
        raise HTTPException(409, f"Il branch «{info.name}» e' {'unito' if info.state == 'merged' else 'chiuso'}: sola lettura")
    if not can_write(info, role, username):
        raise HTTPException(403, f"Il branch personale «{info.name}» e' di {info.owner}: solo il proprietario "
                                 "(o un Admin) puo' scrivere")
    return info


def build_router(ctx: Callable[..., Callable], registry, audit: AuditService, cfg: Settings) -> APIRouter:
    """``ctx(role, use_branch=False)``: contesto di progetto sul PRINCIPALE (``?branch`` ignorato) da ``create_app``."""
    from server.projects import _rmtree
    svc: BranchService = registry.branches
    r = APIRouter(prefix="/api/projects/{pid}/branches", tags=["branch"])

    def archived(pid: int) -> bool:
        with registry.db.scope() as s:
            p = s.get(Project, pid)
            return bool(p and p.archived)

    def load(c, slug: str) -> BranchInfo:                                         # noqa: ANN001
        info = svc.get(c.access.project_id, slug)
        if info is None:
            raise HTTPException(404, "Branch inesistente")
        return info

    def out(c, info: BranchInfo) -> BranchOut:                                    # noqa: ANN001
        return svc.out(info, c.access.role, c.access.principal.username, archived(info.project_id))

    def rec(c, action: str, info: BranchInfo, **details) -> None:                 # noqa: ANN001
        svc.record(c.access.project_id, action, c.access.principal.username, info, c.access.principal.ip, **details)

    # ------------------------------------------------------------------ elenco / dettaglio
    @r.get("", response_model=list[BranchOut])
    def list_branches(c=Depends(ctx(Role.VIEWER, use_branch=False))):
        arch = archived(c.access.project_id)
        infos = svc.rows(c.access.project_id)
        infos.sort(key=lambda b: (b.state != "open", b.id))
        return [svc.out(b, c.access.role, c.access.principal.username, arch) for b in infos]

    @r.get("/{slug}", response_model=BranchOut)
    def get_branch(slug: str, c=Depends(ctx(Role.VIEWER, use_branch=False))):
        return out(c, load(c, slug))

    # ------------------------------------------------------------------ creazione
    @r.post("", response_model=BranchOut, status_code=201)
    def create_branch(body: BranchCreate, c=Depends(ctx(Role.DEVELOPER, use_branch=False))):
        """Crea un branch dalla testa del principale (o da ``from_revision``): revisione 1 = copia di quella revisione."""
        pid, user = c.access.project_id, c.access.principal.username
        if body.kind == "team" and c.access.role < Role.LEAD:
            raise HTTPException(403, "I branch di team li crea un Lead Engineer (o un Admin)")
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(422, "Indica il nome del branch")
        main = registry.runtime(pid)
        if not main.store.initialized:
            raise HTTPException(409, "Master non inizializzato: crea il branch dopo il caricamento iniziale")
        if body.kind == "personal" and svc.count_open_personal(pid, user) >= MAX_PERSONAL_BRANCHES:
            raise HTTPException(409, f"Hai gia' {MAX_PERSONAL_BRANCHES} branch personali aperti su questo progetto "
                                     f"(massimo {MAX_PERSONAL_BRANCHES}): chiudine o eliminane uno")
        with main.mgr._lock:                                                      # noqa: SLF001  (nessuna potatura mentre leggo)
            hist = main.store.history()
            n = body.from_revision if body.from_revision is not None else hist[-1].revision
            if not (1 <= n <= len(hist)):
                raise HTTPException(404, f"Revisione {n} del principale inesistente")
            if hist[n - 1].purged:
                raise HTTPException(409, f"La revisione {n} del principale non e' piu' conservata dal server "
                                         f"(limite di revisioni): scegli una revisione conservata")
            src = hist[n - 1]
            data = main.store.read_revision(n)
        with registry.db.scope() as s:
            if name.casefold() in {n.casefold() for n in s.scalars(select(Branch.name).where(
                    Branch.project_id == pid, Branch.state == "open"))}:
                raise HTTPException(409, f"Esiste gia' un branch aperto chiamato «{name}»")
            base = slugify_branch(name)
            if base in RESERVED_SLUGS:
                raise HTTPException(422, f"Il nome «{name}» e' riservato: scegline un altro")
            slug, k = base, 1
            while s.scalar(select(Branch.id).where(Branch.project_id == pid, Branch.slug == slug)):
                k += 1
                slug = f"{base[:36]}-{k}"
            row = Branch(project_id=pid, name=name, slug=slug, kind=body.kind, owner=user, parent="",
                         base_main_revision=n, sync_revision=1, state="open")
            s.add(row)
            s.flush()
            info = _info(row)
        folder = registry.branch_dir(main.slug, slug)
        try:
            if folder.exists() or folder.is_symlink():                            # resto di una creazione interrotta
                _rmtree(folder)
            brt = registry.runtime(pid, slug)
            brt.store.commit(data, user, f"Creato dal principale (rev {n})", None, filename=f"{src.name}{src.ext}")
        except Exception:
            registry.drop_branch_runtime(pid, slug)
            with registry.db.scope() as s:
                s.query(Branch).filter_by(id=info.id).delete()
            if folder.exists():
                _rmtree(folder)
            raise
        rec(c, "branch_created", info, revision=n, from_revision=n)
        svc.announce(pid, "branch_created", info, f"{user} ha creato il branch «{name}» ({'di team' if info.kind == 'team' else 'personale'})",
                     user, 1)
        return out(c, info)

    # ------------------------------------------------------------------ salvataggio diretto (personale)
    def bctx(role: Role):
        """Contesto sul RUNTIME del branch scelto dal path (non da ``?branch``): permessi di scrittura gia' verificati."""
        base = ctx(role, use_branch=False)

        def dep(slug: str, c=Depends(base)):
            info = load(c, slug)
            if role >= Role.DEVELOPER:
                check_write(info, c.access.role, c.access.principal.username)
            brt = registry.runtime(c.access.project_id, slug)
            dev = brt.mgr.join(c.access.principal.user_id, c.access.principal.username, c.access.role)
            return c, info, brt, dev
        return dep

    @r.post("/{slug}/commit", response_model=MasterInfo)
    async def commit_branch(file: UploadFile = File(...), message: str = Form(""), b=Depends(bctx(Role.DEVELOPER))):
        """Nuova revisione nel branch PERSONALE (proprietario o Admin): tipo/Controller/estensione come per la reimport."""
        c, info, brt, dev = b
        if info.kind != "personal":
            raise HTTPException(422, "Il salvataggio diretto vale solo per i branch personali: in un branch di team "
                                     "si usano le richieste di merge")
        data = await file.read()
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        msg = " ".join((message or "").split()) or DEFAULT_COMMIT_MESSAGE

        def work():
            rev = brt.mgr.commit_direct(dev, data, file.filename or "", msg)
            rec(c, "branch_committed", info, revision=rev.revision, sha256=rev.sha256, size=len(data),
                filename=(file.filename or "")[:120], message=msg[:200])
            svc.cancel_main_merges(info.project_id, info.slug, f"il branch «{info.name}» e' cambiato")
            svc.announce(info.project_id, "branch_updated", info, f"{dev.name} ha salvato la rev {rev.revision} nel branch «{info.name}»",
                         dev.name, rev.revision)
            return brt.mgr.master_info()
        return await run_in_threadpool(work)

    @r.post("/{slug}/save", response_model=ProposalDetail, status_code=201)
    async def save_branch(file: UploadFile = File(...), message: str = Form(""), base_revision: Optional[int] = Form(None),
                          b=Depends(bctx(Role.DEVELOPER))):
        """«Salva su branch» in un branch di TEAM: le tue modifiche (rispetto a ``base_revision``, la revisione del branch da cui
        sei partito; default la testa) vengono unite a quelle gia' nel branch (3-way) e consolidate subito con il solo
        consenso dell'autore. Se ci sono conflitti la richiesta resta aperta da risolvere."""
        c, info, brt, dev = b
        if info.kind != "team":
            raise HTTPException(422, "«save» vale per i branch di team: in un branch personale si usa il salvataggio diretto")
        data = await file.read()
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        msg = " ".join((message or "").split()) or DEFAULT_COMMIT_MESSAGE

        def work():
            base = base_revision if base_revision else brt.store.head().revision
            try:
                p = brt.mgr.submit(dev, data, base, msg, meta={"kind": "save"}, auto=True)
            except SessionError as exc:
                raise HTTPException(exc.status, exc.message) from exc
            rec(c, "branch_committed", info, proposal=p.id, base_revision=base, size=len(data),
                filename=(file.filename or "")[:120], message=msg[:200])
            return brt.mgr.detail(p)
        return await run_in_threadpool(work)

    # ------------------------------------------------------------------ sync dal principale
    @r.post("/{slug}/sync", response_model=ProposalDetail, status_code=201)
    async def sync_branch(b=Depends(bctx(Role.DEVELOPER))):
        """«Aggiorna dal principale»: richiesta di merge NEL branch (base = principale[base], mine = branch, theirs = principale)."""
        c, info, brt, dev = b
        main = registry.runtime(info.project_id)

        def work():
            with main.mgr._lock:                                                  # noqa: SLF001
                if not main.store.initialized:
                    raise HTTPException(409, "Master non inizializzato")
                head = main.store.head().revision
                if head <= info.base_main_revision:
                    raise HTTPException(409, f"Il branch «{info.name}» e' gia' aggiornato rispetto al principale "
                                             f"(rev {info.base_main_revision})")
                if main.store.is_purged(info.base_main_revision) or not main.store.has_revision(info.base_main_revision):
                    raise HTTPException(409, f"La revisione {info.base_main_revision} del principale (base del branch) non e' "
                                             "piu' conservata: impossibile calcolare l'aggiornamento")
                base = main.store.read_revision(info.base_main_revision)
                theirs = main.store.read_revision(head)
            try:
                p = brt.mgr.submit(dev, theirs, info.base_main_revision,
                                   f"Aggiornamento dal principale (rev {head})", base_data=base,
                                   meta={"kind": "sync", "main_head": head}, swap=True)
            except SessionError as exc:
                raise HTTPException(exc.status, exc.message) from exc
            return brt.mgr.detail(p)
        return await run_in_threadpool(work)

    # ------------------------------------------------------------------ merge nel principale
    @r.post("/{slug}/merge", response_model=ProposalDetail, status_code=201)
    async def merge_branch(slug: str, message: str = Form(""), file: Optional[UploadFile] = File(None),
                           c=Depends(ctx(Role.DEVELOPER, use_branch=False))):
        """«Unisci nel principale»: richiesta di merge NEL principale (base = principale[base], file = testa del branch,
        oppure il ``file`` inviato: la versione di lavoro dell'utente, per esempio modificata a partire dal branch)."""
        info = check_write(load(c, slug), c.access.role, c.access.principal.username)
        main, brt = registry.runtime(info.project_id), registry.runtime(info.project_id, slug)
        msg = f"Unione del branch «{info.name}»" + (f": {' '.join(message.split())}" if message.strip() else "")
        sent = await file.read() if file is not None and file.filename else None
        if sent is not None and len(sent) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")

        def work():
            with brt.mgr._lock:                                                   # noqa: SLF001
                head = brt.store.head().revision
                data = sent if sent is not None else brt.store.read_revision(head)
            try:
                p = main.mgr.submit(c.dev, data, info.base_main_revision, msg,
                                    meta={"kind": "merge", "source_branch": slug, "branch_head": head})
            except SessionError as exc:
                raise HTTPException(exc.status, exc.message) from exc
            return main.mgr.detail(p)
        return await run_in_threadpool(work)

    # ------------------------------------------------------------------ anteprima (dry-run)
    @r.post("/{slug}/dry-run", response_model=PreviewOut)
    async def dry_run(slug: str, mode: str = Form("merge"), file: Optional[UploadFile] = File(None),
                      c=Depends(ctx(Role.DEVELOPER, use_branch=False))):
        """Anteprima senza effetti: ``mode=merge`` = «Unisci nel principale» (opz. ``file``), ``mode=sync`` = «Aggiorna dal
        principale». Dice quanti elementi cambiano e quali andrebbero in conflitto."""
        info = load(c, slug)
        if mode not in ("merge", "sync"):
            raise HTTPException(422, "mode deve essere «merge» o «sync»")
        main, brt = registry.runtime(info.project_id), registry.runtime(info.project_id, slug)
        sent = await file.read() if file is not None and file.filename else None

        def work():
            try:
                if mode == "merge":
                    with brt.mgr._lock:                                           # noqa: SLF001
                        data = sent if sent is not None else brt.store.read_revision(brt.store.head().revision)
                    return main.mgr.dry_run(data, info.base_main_revision)
                with main.mgr._lock:                                              # noqa: SLF001
                    if not main.store.initialized:
                        raise HTTPException(409, "Master non inizializzato")
                    head = main.store.head().revision
                    if head <= info.base_main_revision:
                        raise HTTPException(409, f"Il branch «{info.name}» e' gia' aggiornato rispetto al principale")
                    if main.store.is_purged(info.base_main_revision) or not main.store.has_revision(info.base_main_revision):
                        raise HTTPException(409, "La revisione base del branch non e' piu' conservata nel principale")
                    base = main.store.read_revision(info.base_main_revision)
                    theirs = main.store.read_revision(head)
                return brt.mgr.dry_run(theirs, info.base_main_revision, base_data=base, swap=True)
            except SessionError as exc:
                raise HTTPException(exc.status, exc.message) from exc
        return await run_in_threadpool(work)

    # ------------------------------------------------------------------ chiusura / eliminazione
    @r.post("/{slug}/close", response_model=BranchOut)
    def close_branch(slug: str, c=Depends(ctx(Role.DEVELOPER, use_branch=False))):
        """Chiude il branch (sola lettura, resta consultabile). Proprietario, Admin; i Lead anche quelli di team."""
        info = load(c, slug)
        user = c.access.principal.username
        if not can_delete(info, c.access.role, user):
            raise HTTPException(403, f"Solo il proprietario del branch (o un Admin) puo' chiuderlo")
        if info.state != "open":
            raise HTTPException(409, f"Il branch «{info.name}» e' gia' {'unito' if info.state == 'merged' else 'chiuso'}")
        pid = info.project_id
        info = svc._update(pid, slug, state="closed") or info                     # noqa: SLF001
        brt = registry.runtime(pid, slug)
        brt.mgr.cancel_matching(lambda q: True, f"il branch «{info.name}» e' stato chiuso", user)
        svc.cancel_main_merges(pid, slug, f"il branch «{info.name}» e' stato chiuso", user)
        rec(c, "branch_closed", info, revision=brt.store.head().revision if brt.store.initialized else None)
        svc.announce(pid, "branch_updated", info, f"{user} ha chiuso il branch «{info.name}»", user)
        return out(c, info)

    @r.delete("/{slug}", response_model=BranchOut)
    def delete_branch(slug: str, c=Depends(ctx(Role.DEVELOPER, use_branch=False))):
        """Elimina il branch (qualsiasi stato): cartella dei file, metadati, runtime e connessioni WebSocket."""
        info = load(c, slug)
        user = c.access.principal.username
        if not can_delete(info, c.access.role, user):
            raise HTTPException(403, "Solo il proprietario del branch (o un Admin; un Lead per quelli di team) puo' eliminarlo")
        pid = info.project_id
        final = out(c, info)
        svc.cancel_main_merges(pid, slug, f"il branch «{info.name}» e' stato eliminato", user)
        brt = registry.drop_branch_runtime(pid, slug)
        folder = registry.branch_dir(c.access.slug, slug)
        stash = None
        if brt is not None:
            with brt.mgr._lock:                                                   # noqa: SLF001
                if folder.exists() or folder.is_symlink():
                    stash = folder.with_name(f".deleted-{uuid.uuid4().hex[:8]}")
                    import os
                    os.replace(folder, stash)
        elif folder.exists() or folder.is_symlink():
            import os
            stash = folder.with_name(f".deleted-{uuid.uuid4().hex[:8]}")
            os.replace(folder, stash)
        with registry.db.scope() as s:
            s.query(Branch).filter_by(id=info.id).delete()
        if stash is not None:
            try:
                _rmtree(stash)
            except OSError as exc:                                                # pragma: no cover
                log.warning("Cartella temporanea %s non cancellata: %s", stash, exc)
        from server.notifier import branch_key_prefix
        registry.notifier.close_scope(branch_key_prefix(pid, slug), Event(
            type="branch_deleted", message=f"Il branch «{info.name}» e' stato eliminato da {user}", developer=user,
            branch=slug).model_dump(mode="json"))
        rec(c, "branch_deleted", info, state=info.state, head_revision=final.head_revision)
        svc.announce(pid, "branch_deleted", info, f"{user} ha eliminato il branch «{info.name}»", user)
        return final

    return r
