"""Registry dei progetti PLC: un MasterStore + un SessionManager per progetto; route di amministrazione."""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from common.roles import Role
from common.schemas import (Event, PermissionIn, PermissionOut, ProjectCreate, ProjectDeleteOut, ProjectOut,
                            ProjectUpdate)
from server.auth.models import Principal
from server.auth.rbac import effective_project_role, get_principal, min_role
from server.comments import CommentService
from server.config import Settings
from server.db.audit import AuditService
from server.db.base import Database
from server.db.models import Project, ProjectPermission, RoleRow, User
from server.export import build_project_zip
from server.notifier import BranchSink, Notifier, ProjectSink
from server.session_manager import SessionManager
from server.storage import MasterStore

log = logging.getLogger("aps.projects")


class ProjectGone(LookupError):
    """Il progetto e' in corso di eliminazione."""


class BranchNotFound(LookupError):
    """Il branch richiesto non esiste (o e' stato eliminato)."""


@dataclass
class ProjectRuntime:
    project_id: int
    slug: str
    store: MasterStore
    mgr: SessionManager
    branch: Optional[str] = None             # slug del branch (None = principale)
    branch_kind: str = ""                    # team | personal
    branch_owner: str = ""
    branch_name: str = ""


class ProjectRegistry:
    def __init__(self, cfg: Settings, db: Database, notifier: Notifier, audit: AuditService):
        from server.branches import BranchService            # import locale: branches.py usa questo modulo
        self.cfg, self.db, self.notifier, self.audit = cfg, db, notifier, audit
        self._rt: dict[int, ProjectRuntime] = {}
        self._brt: dict[tuple[int, str], ProjectRuntime] = {}      # runtime dei branch: (id progetto, slug del branch)
        self._deleting: set[int] = set()
        self._lock = threading.Lock()
        self.comments = CommentService(db)          # discussione sulle richieste (server/comments.py)
        self.branches = BranchService(self)         # metadati e regole dei branch (server/branches.py)

    def runtimes(self) -> list[ProjectRuntime]:
        """Tutti i runtime caricati: PRIMA i branch e poi i principali (manutenzione periodica, backup).

        Ordine dei lock: chi ne prende piu' d'uno prende prima quelli dei branch e poi quello del principale (i ganci di
        un branch che annullano richieste nel principale lo fanno tenendo il lock del branch)."""
        with self._lock:
            return list(self._brt.values()) + list(self._rt.values())

    def branch_runtimes(self, project_id: int) -> list[ProjectRuntime]:
        with self._lock:
            return [rt for (pid, _), rt in self._brt.items() if pid == project_id]

    def runtime(self, project_id: int, branch: Optional[str] = None) -> ProjectRuntime:
        """Il runtime del principale oppure, con ``branch`` (slug), quello del branch. ``BranchNotFound`` se non esiste."""
        with self._lock:
            if project_id in self._deleting:
                raise ProjectGone(f"Progetto {project_id} in eliminazione")
            if branch:
                return self._branch_runtime(project_id, branch)
            rt = self._rt.get(project_id)
            if rt is None:
                with self.db.scope() as s:
                    row = s.get(Project, project_id)
                    if row is None:
                        raise LookupError(f"Progetto {project_id} inesistente")
                    slug, lead, max_rev = row.slug, row.require_lead_approval, row.max_revisions
                store = MasterStore(self.cfg.data_dir / "projects" / slug)
                mgr = SessionManager(
                    store, sink=ProjectSink(self.notifier, project_id), ttl=self.cfg.presence_ttl,
                    audit=lambda action, actor, target, details, pid=project_id:
                    self.audit.record(action, actor, project_id=pid, target=target, details=details),
                    require_lead_approval=lead, max_revisions=max_rev)
                mgr.comment_count = lambda prop, pid=project_id: self.comments.count(pid, prop)
                mgr.extra_protect = lambda pid=project_id: self.branches.protected_main_revisions(pid)
                mgr.on_merged = lambda p, rev, pid=project_id: self.branches.on_main_merged(pid, p, rev)
                mgr.finalize_guard = lambda p, pid=project_id: self.branches.main_finalize_guard(pid, p)
                rt = self._rt[project_id] = ProjectRuntime(project_id, slug, store, mgr)
                mgr.restore_pending()
            return rt

    def _branch_runtime(self, project_id: int, bslug: str) -> ProjectRuntime:
        """(con ``self._lock`` preso) carica o crea il runtime di un branch dai suoi metadati."""
        rt = self._brt.get((project_id, bslug))
        if rt is not None:
            return rt
        with self.db.scope() as s:
            proj = s.get(Project, project_id)
            if proj is None:
                raise LookupError(f"Progetto {project_id} inesistente")
            slug, lead, max_rev = proj.slug, proj.require_lead_approval, proj.max_revisions
        info = self.branches.get(project_id, bslug)
        if info is None:
            raise BranchNotFound(f"Branch «{bslug}» inesistente")
        store = MasterStore(self.branch_dir(slug, bslug))
        mgr = SessionManager(
            store, sink=BranchSink(self.notifier, project_id, bslug), ttl=self.cfg.presence_ttl,
            audit=lambda action, actor, target, details, pid=project_id, b=bslug, n=info.name:
            self.audit.record(action, actor, project_id=pid, target=target, details={**details, "branch": b, "branch_name": n}),
            require_lead_approval=lead and info.kind == "team", max_revisions=max_rev)
        mgr.auto_approve = info.kind == "personal"
        mgr.comment_count = lambda prop, pid=project_id: self.comments.count(pid, prop)
        mgr.on_merged = lambda p, rev, pid=project_id, b=bslug: self.branches.on_branch_merged(pid, b, p, rev)
        mgr.finalize_guard = lambda p, pid=project_id, b=bslug: self.branches.branch_finalize_guard(pid, b, p)
        rt = self._brt[(project_id, bslug)] = ProjectRuntime(project_id, slug, store, mgr, bslug, info.kind, info.owner, info.name)
        mgr.restore_pending()
        return rt

    def drop_branch_runtime(self, project_id: int, bslug: str) -> Optional[ProjectRuntime]:
        with self._lock:
            return self._brt.pop((project_id, bslug), None)

    def branch_dir(self, slug: str, bslug: str) -> Path:
        """Cartella dati di un branch (``projects/<slug>/branches/<branch>``); rifiuta slug che escono dalla cartella."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", bslug or ""):
            raise ValueError(f"Slug del branch non valido: {bslug!r}")
        return self.project_dir(slug) / "branches" / bslug

    def set_policy(self, project_id: int, require_lead_approval: bool) -> None:
        self.runtime(project_id).mgr.require_lead_approval = require_lead_approval
        for rt in self.branch_runtimes(project_id):
            if rt.branch_kind == "team":                            # nei personali il consenso e' del solo proprietario
                rt.mgr.require_lead_approval = require_lead_approval

    def set_retention(self, project_id: int, max_revisions: int, actor: str = "system") -> list[int]:
        """Imposta il limite di revisioni conservate e pota subito quelle in eccesso. Ritorna le revisioni eliminate.

        Vale anche per i branch aperti (stesso limite del progetto): le basi di fork dei branch restano protette.
        """
        mgr = self.runtime(project_id).mgr
        mgr.max_revisions = max_revisions
        gone = mgr.prune_revisions(actor)
        for info in self.branches.rows(project_id, states=("open",)):
            try:
                bm = self.runtime(project_id, info.slug).mgr
            except LookupError:
                continue
            bm.max_revisions = max_revisions
            bm.prune_revisions(actor)
        return gone

    def purged_count(self, project_id: int) -> int:
        return self.runtime(project_id).store.purged_count()

    def project_dir(self, slug: str) -> Path:
        """Cartella dati del progetto; rifiuta qualunque percorso che non sia direttamente dentro ``data/projects``."""
        root = (self.cfg.data_dir / "projects").resolve()
        d = Path(os.path.normpath(root / slug))
        if not slug or d.parent != root or slug in (".", ".."):
            raise ValueError(f"Percorso del progetto non valido: {slug!r}")
        return d

    def delete(self, project_id: int, actor: str, ip: str = "") -> ProjectDeleteOut:
        """Elimina il progetto: DB (permessi in cascata), cartella dati, audit del progetto e runtime in memoria.

        La cartella viene prima spostata in un nome temporaneo (se fallisce non si cancella nulla) e cancellata alla fine.
        Poi avvisa i client collegati (evento ``project_deleted``) e ne chiude le connessioni. Nell'audit NON resta nessuna
        traccia del progetto (scelta dell'amministratore): l'unica memoria e' una riga nel registro applicativo del server.
        """
        with self._lock:
            if project_id in self._deleting:
                raise ProjectGone(f"Progetto {project_id} in eliminazione")
            self._deleting.add(project_id)
            rt = self._rt.pop(project_id, None)
            brts = {k: self._brt.pop(k) for k in [k for k in self._brt if k[0] == project_id]}     # runtime dei branch
        stash: Optional[Path] = None
        try:
            with self.db.scope() as s:
                row = s.get(Project, project_id)
                if row is None:
                    raise LookupError(f"Progetto {project_id} inesistente")
                name, slug = row.name, row.slug
            folder = self.project_dir(slug)
            with (rt.mgr._lock if rt else contextlib.nullcontext()):                         # noqa: SLF001
                if folder.exists() or folder.is_symlink():
                    stash = folder.with_name(f".deleted-{uuid.uuid4().hex[:8]}")
                    os.replace(folder, stash)
                revs = (len(list((stash / "revisions").glob("rev_*"))) + len(list(stash.glob("branches/*/revisions/rev_*")))
                        ) if stash and not stash.is_symlink() else 0       # anche le revisioni dei branch
                try:
                    with self.db.scope() as s:
                        self.comments.delete_rows(s, project_id)                 # i commenti vanno col progetto
                        self.branches.delete_rows(s, project_id)                 # e i branch (le cartelle sono nella stash)
                        s.delete(s.get(Project, project_id))
                except Exception:
                    if stash is not None:
                        os.replace(stash, folder)
                    raise
        except Exception:
            with self._lock:
                self._deleting.discard(project_id)
                if rt is not None:
                    self._rt[project_id] = rt
                self._brt.update(brts)
            raise
        try:
            self.comments.forget(project_id)
            removed = self.audit.purge_project(project_id)
            log.info("Progetto «%s» (%s) eliminato da %s: %d righe di audit e %d revisioni rimosse",
                     name, slug, actor, removed, revs)
            self.notifier.close_project(project_id, Event(
                type="project_deleted", message=f"Il progetto «{name}» e' stato eliminato da {actor}",
                developer=actor).model_dump(mode="json"))
            if stash is not None:
                try:
                    _rmtree(stash)
                except OSError as exc:
                    log.warning("Cartella temporanea %s non cancellata: %s", stash, exc)
        finally:
            with self._lock:
                self._deleting.discard(project_id)
        return ProjectDeleteOut(id=project_id, name=name, audit_entries_removed=removed, revisions_removed=revs)

    def head_revision(self, project_id: int) -> Optional[int]:
        st = self.runtime(project_id).store
        return st.head().revision if st.initialized else None

    def master_format(self, project_id: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
        st = self.runtime(project_id).store
        return (st.kind, st.ext, st.name) if st.initialized else (None, None, None)


def _rmtree(path: Path) -> None:
    """Cancella una cartella (o un link simbolico, senza seguirlo); i file di sola lettura vengono sbloccati."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return

    def unlock(func, p, _exc):                                  # noqa: ANN001
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=unlock)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "progetto")[:60]


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/projects", tags=["progetti"])

    def out(request: Request, s, proj: Project, role: Optional[Role], new: bool = False) -> ProjectOut:   # noqa: ANN001
        reg = request.app.state.registry
        kind, ext, mname = (None, None, None) if new else reg.master_format(proj.id)
        return ProjectOut(id=proj.id, slug=proj.slug, name=proj.name, description=proj.description,
                          require_lead_approval=proj.require_lead_approval, archived=proj.archived,
                          my_role=role.key if role else None,
                          head_revision=None if new else reg.head_revision(proj.id),
                          master_kind=kind, master_ext=ext, master_name=mname, max_revisions=proj.max_revisions,
                          purged_revisions=0 if new else reg.purged_count(proj.id))

    @r.get("", response_model=list[ProjectOut])
    def list_projects(request: Request, p: Principal = Depends(min_role(Role.VIEWER))):
        with request.app.state.db.scope() as s:
            user = s.get(User, p.user_id)
            res = []
            for proj in s.scalars(select(Project).order_by(Project.name)):
                role = effective_project_role(s, user, proj.id)
                if role is not None:
                    try:
                        res.append(out(request, s, proj, role))
                    except ProjectGone:                      # eliminato proprio ora
                        continue
            return res

    @r.post("", response_model=ProjectOut, status_code=201)
    def create_project(body: ProjectCreate, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        slug = body.slug or _slugify(body.name)
        with request.app.state.db.scope() as s:
            base, n = slug, 1
            while s.scalar(select(Project.id).where(Project.slug == slug)):
                if body.slug:
                    raise HTTPException(409, f"Slug '{slug}' gia' in uso")
                n += 1
                slug = f"{base}-{n}"
            max_rev = body.max_revisions or min(max(request.app.state.settings.default_max_revisions, 2), 200)
            proj = Project(slug=slug, name=body.name.strip(), description=body.description,
                           require_lead_approval=body.require_lead_approval, max_revisions=max_rev,
                           created_by=p.username)
            s.add(proj)
            s.flush()
            res = out(request, s, proj, Role.ADMIN, new=True)
        request.app.state.audit.record("project_created", p.username, project_id=res.id, target=res.slug,
                                       ip=p.ip, details={"name": res.name})
        return res

    @r.patch("/{pid}", response_model=ProjectOut)
    def update_project(pid: int, body: ProjectUpdate, request: Request,
                       p: Principal = Depends(min_role(Role.ADMIN))):
        changed, old_max = {}, None
        reg, audit = request.app.state.registry, request.app.state.audit
        with request.app.state.db.scope() as s:
            proj = s.get(Project, pid)
            if proj is None:
                raise HTTPException(404, "Progetto inesistente")
            for k in ("name", "description", "require_lead_approval", "archived", "max_revisions"):
                v = getattr(body, k)
                if v is not None and v != getattr(proj, k):
                    if k == "max_revisions":
                        old_max = proj.max_revisions
                    setattr(proj, k, v)
                    changed[k] = v
            slug = proj.slug
        if "require_lead_approval" in changed:
            reg.set_policy(pid, changed["require_lead_approval"])
        if "max_revisions" in changed:
            audit.record("retention_changed", p.username, project_id=pid, target=slug, ip=p.ip,
                         details={"old": old_max, "new": changed["max_revisions"]})
            reg.set_retention(pid, changed["max_revisions"], p.username)       # pota subito e scrive "revisions_pruned"
        others = {k: v for k, v in changed.items() if k != "max_revisions"}
        if others or not changed:
            audit.record("project_updated", p.username, project_id=pid, target=slug, ip=p.ip, details=others)
        if changed:
            request.app.state.notifier.publish(
                Event(type="project_updated", message=f"Progetto «{slug}» aggiornato da {p.username}",
                      developer=p.username).model_dump(mode="json"), None, scope=f"{pid}:")
        with request.app.state.db.scope() as s:
            return out(request, s, s.get(Project, pid), Role.ADMIN)

    @r.delete("/{pid}", response_model=ProjectDeleteOut)
    def delete_project(pid: int, request: Request, confirm: str = Query(""),
                       p: Principal = Depends(min_role(Role.ADMIN))):
        """Elimina il progetto con tutti i suoi dati e il suo audit. ``confirm`` deve essere il nome ESATTO del progetto."""
        with request.app.state.db.scope() as s:
            proj = s.get(Project, pid)
            if proj is None:
                raise HTTPException(404, "Progetto inesistente")
            name = proj.name
        if confirm != name:
            raise HTTPException(422, "Per eliminare il progetto scrivi il suo nome esatto come conferma")
        try:
            return request.app.state.registry.delete(pid, p.username, p.ip)
        except LookupError as exc:
            raise HTTPException(404, "Progetto inesistente") from exc

    @r.get("/{pid}/export")
    def export_project(pid: int, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        """Esporta il progetto in uno .zip: revisioni conservate, history.json, audit.csv, README.txt."""
        app = request.app
        with app.state.db.scope() as s:
            proj = s.get(Project, pid)
            if proj is None:
                raise HTTPException(404, "Progetto inesistente")
            name, slug, max_rev = proj.name, proj.slug, proj.max_revisions
        reg = app.state.registry
        rt = reg.runtime(pid)
        brs = [(b, reg.runtime(pid, b.slug)) for b in reg.branches.rows(pid)]
        with contextlib.ExitStack() as locks:                          # prima i lock dei branch, poi quello del principale
            for _b, brt in brs:
                locks.enter_context(brt.mgr._lock)                     # noqa: SLF001  (nessuna potatura durante la lettura)
            locks.enter_context(rt.mgr._lock)                          # noqa: SLF001
            rows = app.state.audit.for_project(pid)
            f = build_project_zip(name, slug, rt.store, rows, max_rev,
                                  branches=[(b.__dict__, brt.store) for b, brt in brs])
            n_rev, n_purged = len(rt.store.kept_revisions()), rt.store.purged_count()
        app.state.audit.record("project_exported", p.username, project_id=pid, target=slug, ip=p.ip,
                               details={"revisions": n_rev, "purged": n_purged, "audit_entries": len(rows),
                                        **({"branches": len(brs)} if brs else {})})

        def chunks():
            try:
                while block := f.read(256 * 1024):
                    yield block
            finally:
                f.close()
        fname = f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', slug) or 'progetto'}_{datetime.now():%Y%m%d_%H%M}.zip"
        return StreamingResponse(chunks(), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # --------------------------------------------------------------- permessi sul progetto
    @r.get("/{pid}/permissions", response_model=list[PermissionOut])
    def list_permissions(pid: int, request: Request, _: Principal = Depends(min_role(Role.ADMIN))):
        with request.app.state.db.scope() as s:
            if s.get(Project, pid) is None:
                raise HTTPException(404, "Progetto inesistente")
            perms = {pp.user_id: pp for pp in s.scalars(select(ProjectPermission).where(ProjectPermission.project_id == pid))}
            res = []
            for u in s.scalars(select(User).where(User.is_active.is_(True)).order_by(User.username)):
                pp, account = perms.get(u.id), Role(u.role.rank)
                eff = effective_project_role(s, u, pid)
                res.append(PermissionOut(user_id=u.id, username=u.username, display_name=u.display_name,
                                         account_role=account.key, role=Role(pp.role.rank).key if pp else None,
                                         effective_role=eff.key if eff else None))
            return res

    @r.put("/{pid}/permissions", response_model=list[PermissionOut])
    def set_permission(pid: int, body: PermissionIn, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        with request.app.state.db.scope() as s:
            proj, user = s.get(Project, pid), s.get(User, body.user_id)
            if proj is None or user is None:
                raise HTTPException(404, "Progetto o utente inesistente")
            perm = s.scalar(select(ProjectPermission).where(ProjectPermission.project_id == pid,
                                                            ProjectPermission.user_id == body.user_id))
            if body.role is None:
                if perm:
                    s.delete(perm)
                action, new = "permission_revoked", None
            else:
                try:
                    role = Role.parse(body.role)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                if role > Role(user.role.rank):
                    raise HTTPException(422, f"Il ruolo sul progetto non puo' superare il ruolo dell'account "
                                             f"({Role(user.role.rank).key})")
                if role >= Role.ADMIN:
                    raise HTTPException(422, "Il ruolo Admin e' globale: va assegnato sull'account")
                rid = s.scalar(select(RoleRow.id).where(RoleRow.rank == int(role)))
                if perm:
                    perm.role_id, perm.granted_by = rid, p.username
                else:
                    s.add(ProjectPermission(user_id=user.id, project_id=pid, role_id=rid, granted_by=p.username))
                action, new = "permission_granted", role.key
            uname, slug = user.username, proj.slug
        request.app.state.audit.record(action, p.username, project_id=pid, target=uname, ip=p.ip,
                                       details={"project": slug, "role": new})
        return list_permissions(pid, request, p)

    return r
