"""Endpoint di autenticazione, sessioni, utenti e ruoli."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from common.roles import Role
from common.schemas import (AccountPolicy, AuthSessionOut, LoginIn, PasswordChange, RefreshIn, RoleOut, TokenPair,
                            UserCreate, UserOut, UserUpdate)
from server.auth.models import Principal
from server.auth.rbac import get_principal, min_role


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api", tags=["auth"])

    def svc(request: Request):
        return request.app.state.auth

    @r.post("/auth/login", response_model=TokenPair)
    def login(body: LoginIn, request: Request):
        return svc(request).login(body.username, body.password,
                                  ip=request.client.host if request.client else "",
                                  client=request.headers.get("User-Agent", ""))

    @r.post("/auth/refresh", response_model=TokenPair)
    def refresh(body: RefreshIn, request: Request):
        return svc(request).refresh(body.refresh_token, ip=request.client.host if request.client else "")

    @r.post("/auth/logout", status_code=204)
    def logout(request: Request, p: Principal = Depends(get_principal)):
        svc(request).logout(p)

    @r.get("/auth/me", response_model=UserOut)
    def me(request: Request, p: Principal = Depends(get_principal)):
        return svc(request).me(p)

    @r.post("/auth/change-password", status_code=204)
    def change_password(body: PasswordChange, request: Request, p: Principal = Depends(get_principal)):
        svc(request).change_password(p, body.old_password, body.new_password)

    @r.get("/auth/sessions", response_model=list[AuthSessionOut])
    def sessions(request: Request, all: bool = False, p: Principal = Depends(get_principal)):
        return svc(request).list_sessions(p, all_users=all)

    @r.delete("/auth/sessions/{sid}", status_code=204)
    def revoke(sid: str, request: Request, p: Principal = Depends(get_principal)):
        svc(request).revoke_session(p, sid)

    @r.get("/auth/policy", response_model=AccountPolicy)
    def get_policy(request: Request, _: Principal = Depends(get_principal)):
        return svc(request).policy()

    @r.put("/auth/policy", response_model=AccountPolicy)
    def set_policy(body: AccountPolicy, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        return svc(request).set_policy(p, body)

    @r.get("/roles", response_model=list[RoleOut])
    def roles(request: Request, _: Principal = Depends(min_role(Role.VIEWER))):
        return svc(request).roles()

    @r.get("/users", response_model=list[UserOut])
    def users(request: Request, _: Principal = Depends(min_role(Role.ADMIN))):
        return svc(request).list_users()

    @r.post("/users", response_model=UserOut, status_code=201)
    def create_user(body: UserCreate, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        return svc(request).create_user(p, body)

    @r.patch("/users/{uid}", response_model=UserOut)
    def update_user(uid: int, body: UserUpdate, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        return svc(request).update_user(p, uid, body)

    @r.delete("/users/{uid}", status_code=204)
    def delete_user(uid: int, request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        svc(request).delete_user(p, uid)

    return r
