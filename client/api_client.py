"""Client HTTP sincrono verso il Automation Project Server (JWT con refresh automatico).

Pensato per essere eseguito in thread di lavoro (vedi ``client/workers.py``). Tutte le chiamate di
progetto usano ``self.project_id`` (impostato dalla GUI con ``set_project``).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from common.roles import Role
from common.schemas import (AuditOut, AuditVerifyOut, AuthSessionOut, DecisionIn, DeveloperOut, MasterInfo,
                            PermissionIn, PermissionOut, ProjectCreate, ProjectOut, ProjectUpdate,
                            ProposalDetail, ProposalSummary, ResolutionIn, ResolutionsIn, RestoreIn, RoleOut,
                            TokenPair, UserCreate, UserOut, UserUpdate)


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"[{status}] {detail}")
        self.status, self.detail = status, detail


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout,
                                  headers={"User-Agent": "AutomationProjectClient/2.0"})
        self.tokens: Optional[TokenPair] = None
        self._expires_at = 0.0
        self._refresh_lock = threading.Lock()
        self.project_id: Optional[int] = None

    # ------------------------------------------------------------------ stato
    @property
    def user(self) -> Optional[UserOut]:
        return self.tokens.user if self.tokens else None

    @property
    def logged_in(self) -> bool:
        return self.tokens is not None

    @property
    def role(self) -> Role:
        return Role.parse(self.user.role) if self.user else Role.VIEWER

    @property
    def session_key(self) -> str:
        """Identificativo di presenza usato dal server nei partecipanti (``u<user_id>``)."""
        return f"u{self.user.id}" if self.user else ""

    def set_project(self, project_id: Optional[int]) -> None:
        self.project_id = project_id

    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/ws?token={self.tokens.access_token}&project={self.project_id}"

    # ---------------------------------------------------------------- trasporto
    def _store(self, pair: TokenPair) -> TokenPair:
        self.tokens = pair
        self._expires_at = time.monotonic() + pair.expires_in
        return pair

    def _refresh(self) -> None:
        with self._refresh_lock:
            if not self.tokens or time.monotonic() < self._expires_at - 30:      # gia' rinnovato da un altro thread
                return
            try:
                r = self._http.post("/api/auth/refresh", json={"refresh_token": self.tokens.refresh_token})
            except httpx.HTTPError as exc:
                raise ApiError(0, f"Server non raggiungibile: {exc}") from exc
            if r.status_code != 200:
                self.tokens = None
                raise ApiError(401, "Sessione scaduta: effettua di nuovo il login")
            self._store(TokenPair(**r.json()))

    def _req(self, method: str, url: str, *, auth: bool = True, **kw) -> httpx.Response:
        if auth and self.tokens and time.monotonic() >= self._expires_at - 30:
            self._refresh()
        headers = {"Authorization": f"Bearer {self.tokens.access_token}"} if (auth and self.tokens) else {}
        try:
            r = self._http.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"Server non raggiungibile: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            if isinstance(detail, list):                       # errori di validazione pydantic
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise ApiError(r.status_code, str(detail))
        return r

    def _p(self, path: str = "") -> str:
        if self.project_id is None:
            raise ApiError(0, "Nessun progetto selezionato")
        return f"/api/projects/{self.project_id}{path}"

    # ------------------------------------------------------------------- auth
    def health(self) -> bool:
        try:
            return self._http.get("/api/health", timeout=4).status_code == 200
        except httpx.HTTPError:
            return False

    def login(self, username: str, password: str) -> TokenPair:
        r = self._req("POST", "/api/auth/login", auth=False, json={"username": username, "password": password})
        return self._store(TokenPair(**r.json()))

    def logout(self) -> None:
        if self.tokens:
            try:
                self._req("POST", "/api/auth/logout")
            finally:
                self.tokens = None

    def me(self) -> UserOut:
        return UserOut(**self._req("GET", "/api/auth/me").json())

    def change_password(self, old: str, new: str) -> None:
        self._req("POST", "/api/auth/change-password", json={"old_password": old, "new_password": new})
        if self.tokens:
            self.tokens.user.must_change_password = False

    def sessions(self, all_users: bool = False) -> list[AuthSessionOut]:
        return [AuthSessionOut(**s) for s in self._req("GET", "/api/auth/sessions", params={"all": all_users}).json()]

    def revoke_session(self, sid: str) -> None:
        self._req("DELETE", f"/api/auth/sessions/{sid}")

    # --------------------------------------------------------- utenti / progetti
    def roles(self) -> list[RoleOut]:
        return [RoleOut(**r) for r in self._req("GET", "/api/roles").json()]

    def users(self) -> list[UserOut]:
        return [UserOut(**u) for u in self._req("GET", "/api/users").json()]

    def create_user(self, data: UserCreate) -> UserOut:
        return UserOut(**self._req("POST", "/api/users", json=data.model_dump()).json())

    def update_user(self, uid: int, data: UserUpdate) -> UserOut:
        return UserOut(**self._req("PATCH", f"/api/users/{uid}", json=data.model_dump(exclude_none=True)).json())

    def projects(self) -> list[ProjectOut]:
        return [ProjectOut(**p) for p in self._req("GET", "/api/projects").json()]

    def create_project(self, data: ProjectCreate) -> ProjectOut:
        return ProjectOut(**self._req("POST", "/api/projects", json=data.model_dump(exclude_none=True)).json())

    def update_project(self, pid: int, data: ProjectUpdate) -> ProjectOut:
        return ProjectOut(**self._req("PATCH", f"/api/projects/{pid}", json=data.model_dump(exclude_none=True)).json())

    def permissions(self, pid: int) -> list[PermissionOut]:
        return [PermissionOut(**p) for p in self._req("GET", f"/api/projects/{pid}/permissions").json()]

    def set_permission(self, pid: int, user_id: int, role: Optional[str]) -> list[PermissionOut]:
        r = self._req("PUT", f"/api/projects/{pid}/permissions", json=PermissionIn(user_id=user_id, role=role).model_dump())
        return [PermissionOut(**p) for p in r.json()]

    def init_master_for(self, pid: int, file: Path) -> MasterInfo:
        with open(file, "rb") as f:
            r = self._req("POST", f"/api/projects/{pid}/master/init", files={"file": (file.name, f, "application/octet-stream")})
        return MasterInfo(**r.json())

    # -------------------------------------------------------------------- audit
    def audit(self, project_id: Optional[int] = None, action: Optional[str] = None, actor: Optional[str] = None,
              limit: int = 300) -> list[AuditOut]:
        params = {k: v for k, v in dict(project_id=project_id, action=action, actor=actor, limit=limit).items() if v}
        return [AuditOut(**a) for a in self._req("GET", "/api/audit", params=params).json()]

    def audit_verify(self) -> AuditVerifyOut:
        return AuditVerifyOut(**self._req("GET", "/api/audit/verify").json())

    # ------------------------------------------------------------------- master
    def master_info(self) -> MasterInfo:
        return MasterInfo(**self._req("GET", self._p("/master")).json())

    def history(self) -> list[MasterInfo]:
        return [MasterInfo(**m) for m in self._req("GET", self._p("/master/history")).json()]

    def download_master(self, dest: Path, revision: Optional[int] = None) -> int:
        r = self._req("GET", self._p("/master/download"), params={"revision": revision} if revision else None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return int(r.headers["X-Revision"])

    def restore(self, revision: int, reason: str) -> MasterInfo:
        return MasterInfo(**self._req("POST", self._p("/master/restore"),
                                      json=RestoreIn(revision=revision, reason=reason).model_dump()).json())

    def report_revision(self, revision: Optional[int] = None) -> None:
        """Heartbeat di presenza (+ revisione scaricata): rende l'utente 'attivo' per il quorum."""
        self._req("POST", self._p("/heartbeat"), params={"revision": revision} if revision else None)

    def developers(self) -> list[DeveloperOut]:
        return [DeveloperOut(**d) for d in self._req("GET", self._p("/developers")).json()]

    # ----------------------------------------------------------------- proposte
    def submit_proposal(self, file: Path, base_revision: int, message: str = "") -> ProposalDetail:
        with open(file, "rb") as f:
            r = self._req("POST", self._p("/proposals"), data={"base_revision": base_revision, "message": message},
                          files={"file": (file.name, f, "application/xml")})
        return ProposalDetail(**r.json())

    def proposals(self, active_only: bool = False) -> list[ProposalSummary]:
        return [ProposalSummary(**p) for p in self._req("GET", self._p("/proposals"),
                                                        params={"active_only": active_only}).json()]

    def proposal(self, pid: str) -> ProposalDetail:
        return ProposalDetail(**self._req("GET", self._p(f"/proposals/{pid}")).json())

    def save_preview(self, pid: str, dest: Path) -> None:
        dest.write_bytes(self._req("GET", self._p(f"/proposals/{pid}/preview")).content)

    def set_resolutions(self, pid: str, resolutions: dict[str, ResolutionIn]) -> ProposalDetail:
        body = ResolutionsIn(resolutions=resolutions).model_dump(mode="json")
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/resolutions"), json=body).json())

    def approve(self, pid: str, comment: str = "") -> ProposalDetail:
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/approve"),
                                          json=DecisionIn(comment=comment).model_dump()).json())

    def reject(self, pid: str, comment: str = "") -> ProposalDetail:
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/reject"),
                                          json=DecisionIn(comment=comment).model_dump()).json())

    def cancel(self, pid: str) -> ProposalDetail:
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/cancel")).json())

    def force_merge(self, pid: str, comment: str) -> ProposalDetail:
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/force-merge"),
                                          json=DecisionIn(comment=comment).model_dump()).json())
