"""Controllo accessi basato sui ruoli (RBAC).

Tre modi equivalenti per proteggere una route:

* ``@require_role("Lead_Engineer")``                         decorator (stile richiesto dalla specifica)
* ``Depends(min_role("Lead_Engineer"))``                      dependency FastAPI (usata nelle route del server)
* ``Depends(project_access(Role.DEVELOPER))``                 ruolo *sul singolo progetto* (path ``{pid}``)

La gerarchia e' ``Viewer < Developer < Lead_Engineer < Admin``: ``require_role("Lead_Engineer")`` ammette
anche gli Admin. Il ruolo viene sempre riletto dal DB: una revoca ha effetto immediato.
"""
from __future__ import annotations

import functools
import inspect
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from common.roles import Role
from server.auth.models import AuthError, Principal, ProjectAccess
from server.db.models import Project, ProjectPermission, User

_bearer = HTTPBearer(auto_error=False, description="Access token JWT ottenuto da /api/auth/login")


def get_principal(request: Request,
                  creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Principal:
    """Autentica la richiesta (nessun controllo sul ruolo)."""
    if creds is None:
        raise HTTPException(401, "Autenticazione richiesta", headers={"WWW-Authenticate": "Bearer"})
    try:
        return request.app.state.auth.principal_from_access(
            creds.credentials, ip=request.client.host if request.client else "")
    except AuthError as exc:
        raise HTTPException(exc.status, exc.message, headers={"WWW-Authenticate": "Bearer"}) from exc


def _check_password_flag(p: Principal) -> None:
    if p.must_change_password:
        raise HTTPException(403, "Cambio password obbligatorio: usa /api/auth/change-password")


def min_role(role: "Role | str") -> Callable[..., Principal]:
    """Dependency: richiede un ruolo account >= ``role``."""
    need = Role.parse(role)

    def dep(p: Principal = Depends(get_principal)) -> Principal:
        _check_password_flag(p)
        if p.role < need:
            raise HTTPException(403, f"Permesso negato: serve il ruolo {need.label} o superiore")
        return p

    dep.required_role = need                                   # type: ignore[attr-defined]
    return dep


def require_role(role: "Role | str") -> Callable:
    """Decorator per route FastAPI::

        @router.post("/x")
        @require_role("Lead_Engineer")
        def handler(...): ...

    Il Principal e' iniettato come dependency (non compare tra i parametri della funzione): se serve
    nella funzione, usare ``Depends(get_principal)``.
    """
    dep = min_role(role)

    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        extra = inspect.Parameter("_rbac_principal", inspect.Parameter.KEYWORD_ONLY,
                                  default=Depends(dep), annotation=Principal)
        params = [p for p in sig.parameters.values() if p.kind != inspect.Parameter.VAR_KEYWORD] + [extra]
        params += [p for p in sig.parameters.values() if p.kind == inspect.Parameter.VAR_KEYWORD]

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, _rbac_principal=None, **kw):
                return await fn(*a, **kw)
        else:
            @functools.wraps(fn)
            def wrapper(*a, _rbac_principal=None, **kw):
                return fn(*a, **kw)
        wrapper.__signature__ = sig.replace(parameters=params)         # type: ignore[attr-defined]
        return wrapper

    return deco


# ------------------------------------------------------------------ ruolo sul singolo progetto
def effective_project_role(session, user: User, project_id: int) -> Optional[Role]:   # noqa: ANN001
    """Admin -> Admin ovunque; altrimenti min(ruolo account, ruolo assegnato sul progetto); None = nessun accesso."""
    account = Role(user.role.rank)
    if account >= Role.ADMIN:
        return Role.ADMIN
    perm = session.query(ProjectPermission).filter_by(user_id=user.id, project_id=project_id).one_or_none()
    if perm is None:
        return None
    return min(account, Role(perm.role.rank))


def project_access(role: "Role | str" = Role.VIEWER) -> Callable[..., ProjectAccess]:
    """Dependency per route ``/api/projects/{pid}/...``: verifica l'accesso e il ruolo minimo sul progetto."""
    need = Role.parse(role)

    def dep(pid: int, request: Request, p: Principal = Depends(get_principal)) -> ProjectAccess:
        _check_password_flag(p)
        db = request.app.state.db
        with db.scope() as s:
            proj = s.get(Project, pid)
            user = s.get(User, p.user_id)
            if proj is None:
                raise HTTPException(404, "Progetto inesistente")
            r = effective_project_role(s, user, pid)
            if r is None:
                raise HTTPException(403, "Nessun accesso a questo progetto")
            if proj.archived and need > Role.VIEWER:
                raise HTTPException(409, "Progetto archiviato: sola lettura")
            if r < need:
                raise HTTPException(403, f"Permesso negato: sul progetto serve il ruolo {need.label} o superiore")
            return ProjectAccess(p, proj.id, proj.slug, proj.name, r, proj.require_lead_approval)

    return dep
