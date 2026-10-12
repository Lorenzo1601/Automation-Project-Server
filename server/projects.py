"""Registry dei progetti PLC: un MasterStore + un SessionManager per progetto; route di amministrazione."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from common.roles import Role
from common.schemas import PermissionIn, PermissionOut, ProjectCreate, ProjectOut, ProjectUpdate
from server.auth.models import Principal
from server.auth.rbac import effective_project_role, get_principal, min_role
from server.config import Settings
from server.db.audit import AuditService
from server.db.base import Database
from server.db.models import Project, ProjectPermission, RoleRow, User
from server.notifier import Notifier, ProjectSink
from server.session_manager import SessionManager
from server.storage import MasterStore


@dataclass
class ProjectRuntime:
    project_id: int
    slug: str
    store: MasterStore
    mgr: SessionManager


class ProjectRegistry:
    def __init__(self, cfg: Settings, db: Database, notifier: Notifier, audit: AuditService):
        self.cfg, self.db, self.notifier, self.audit = cfg, db, notifier, audit
        self._rt: dict[int, ProjectRuntime] = {}
        self._lock = threading.Lock()

    def runtime(self, project_id: int) -> ProjectRuntime:
        with self._lock:
            rt = self._rt.get(project_id)
            if rt is None:
                with self.db.scope() as s:
                    row = s.get(Project, project_id)
                    if row is None:
                        raise LookupError(f"Progetto {project_id} inesistente")
                    slug, lead = row.slug, row.require_lead_approval
                store = MasterStore(self.cfg.data_dir / "projects" / slug)
                mgr = SessionManager(
                    store, sink=ProjectSink(self.notifier, project_id), ttl=self.cfg.presence_ttl,
                    audit=lambda action, actor, target, details, pid=project_id:
                    self.audit.record(action, actor, project_id=pid, target=target, details=details),
                    require_lead_approval=lead)
                rt = self._rt[project_id] = ProjectRuntime(project_id, slug, store, mgr)
            return rt

    def set_policy(self, project_id: int, require_lead_approval: bool) -> None:
        self.runtime(project_id).mgr.require_lead_approval = require_lead_approval

    def head_revision(self, project_id: int) -> Optional[int]:
        st = self.runtime(project_id).store
        return st.head().revision if st.initialized else None

    def master_format(self, project_id: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
        st = self.runtime(project_id).store
        return (st.kind, st.ext, st.name) if st.initialized else (None, None, None)


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
                          master_kind=kind, master_ext=ext, master_name=mname)

    @r.get("", response_model=list[ProjectOut])
    def list_projects(request: Request, p: Principal = Depends(min_role(Role.VIEWER))):
        with request.app.state.db.scope() as s:
            user = s.get(User, p.user_id)
            res = []
            for proj in s.scalars(select(Project).order_by(Project.name)):
                role = effective_project_role(s, user, proj.id)
                if role is not None:
                    res.append(out(request, s, proj, role))
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
            proj = Project(slug=slug, name=body.name.strip(), description=body.description,
                           require_lead_approval=body.require_lead_approval, created_by=p.username)
            s.add(proj)
            s.flush()
            res = out(request, s, proj, Role.ADMIN, new=True)
        request.app.state.audit.record("project_created", p.username, project_id=res.id, target=res.slug,
                                       ip=p.ip, details={"name": res.name})
        return res

    @r.patch("/{pid}", response_model=ProjectOut)
    def update_project(pid: int, body: ProjectUpdate, request: Request,
                       p: Principal = Depends(min_role(Role.ADMIN))):
        changed = {}
        with request.app.state.db.scope() as s:
            proj = s.get(Project, pid)
            if proj is None:
                raise HTTPException(404, "Progetto inesistente")
            for k in ("name", "description", "require_lead_approval", "archived"):
                v = getattr(body, k)
                if v is not None and v != getattr(proj, k):
                    setattr(proj, k, v)
                    changed[k] = v
            s.flush()
            res = out(request, s, proj, Role.ADMIN)
        if "require_lead_approval" in changed:
            request.app.state.registry.set_policy(pid, changed["require_lead_approval"])
        request.app.state.audit.record("project_updated", p.username, project_id=pid, target=res.slug,
                                       ip=p.ip, details=changed)
        return res

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
