"""Client HTTP sincrono verso il Automation Project Server (JWT con refresh automatico).

Pensato per essere eseguito in thread di lavoro (vedi ``client/workers.py``). Tutte le chiamate di
progetto usano ``self.project_id`` (impostato dalla GUI con ``set_project``).
"""
from __future__ import annotations

import os
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from client import trust as _trust
from common.roles import Role
from common.schemas_branches import BranchCreate, BranchOut
from common.schemas_inbox import NotificationOut, UpdateInfo
from common.schemas_revisions import CompareOut, LockIn, LockOut, RevisionPatchIn
from common.schemas import (AccountPolicy, PreviewOut, AuditOut, AuditVerifyOut, AuthSessionOut, DecisionIn, DeveloperOut, MasterInfo, PresenceContext, PresenceOut,
                            PermissionIn, PermissionOut, ProjectCreate, ProjectDeleteOut, ProjectOut, ProjectUpdate,
                            ProposalDetail, ProposalSummary, ResolutionIn, ResolutionsIn, RestoreIn, RoleOut,
                            TokenPair, UserCreate, UserOut, UserUpdate)
from common.schemas_comments import CommentEdit, CommentIn, CommentOut


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"[{status}] {detail}")
        self.status, self.detail = status, detail


class TlsTrustError(ApiError):
    """Problema di fiducia sul certificato HTTPS del server.

    ``kind``: ``untrusted`` (certificato non verificabile: si puo' chiedere all'utente di considerarlo attendibile) oppure
    ``changed`` (l'impronta non coincide con quella salvata: possibile attacco o certificato rigenerato).
    """

    def __init__(self, kind: str, host: str, port: int, detail: str, expected: str = "", actual: str = ""):
        super().__init__(0, detail)
        self.kind, self.host, self.port, self.expected, self.actual = kind, host, port, expected, actual


_VERIFY_IT = {"self-signed certificate": "autofirmato", "self signed certificate": "autofirmato",
              "certificate has expired": "scaduto", "unable to get local issuer certificate": "emittente sconosciuto",
              "self-signed certificate in certificate chain": "emittente sconosciuto"}


def _exc_chain(exc: BaseException):
    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        yield e
        e = e.__cause__ or e.__context__


def tls_problem(exc: BaseException, base_url: str) -> Optional[TlsTrustError]:
    """Riconosce nella catena di un errore httpx un problema di certificato (pin cambiato / non verificabile)."""
    host, port = _trust.host_port(base_url)
    for e in _exc_chain(exc):
        if isinstance(e, _trust.PinMismatch):
            return TlsTrustError("changed", host, port, _trust.changed_message(host, port, e.expected, e.actual),
                                 e.expected, e.actual)
        if isinstance(e, ssl.SSLCertVerificationError):
            msg = str(getattr(e, "verify_message", "") or "")
            reason = _VERIFY_IT.get(msg.lower(), "hostname non corrispondente" if "hostname" in msg.lower() or "ip address" in msg.lower() else msg)
            return TlsTrustError("untrusted", host, port, _trust.untrusted_message(host, port, reason))
    return None


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 60.0, trust: Optional[_trust.TrustStore] = None):
        self.base_url = base_url.rstrip("/")
        self.trust = trust
        self.tls_issue: Optional[TlsTrustError] = None       # ultimo problema di certificato incontrato (es. da health())
        kw = {}
        ctx = _trust.context_for(self.base_url, trust)       # https: fiducia di sistema oppure pin salvato; http: None
        if ctx is not None:
            kw["verify"] = ctx
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout,
                                  headers={"User-Agent": "AutomationProjectClient/2.0"}, **kw)
        self.tokens: Optional[TokenPair] = None
        self._expires_at = 0.0
        self._refresh_lock = threading.Lock()
        self.project_id: Optional[int] = None
        self.branch: Optional[str] = None          # slug del branch su cui operano le chiamate di progetto (None = principale)

    # ------------------------------------------------------------------ stato
    @property
    def user(self) -> Optional[UserOut]:
        return self.tokens.user if self.tokens else None

    @property
    def logged_in(self) -> bool:
        return self.tokens is not None

    @property
    def secure(self) -> bool:
        """True se la connessione e' cifrata (https)."""
        return _trust.is_secure_url(self.base_url)

    def _transport_error(self, exc: httpx.HTTPError) -> ApiError:
        """Traduce un errore di rete httpx: problemi di certificato -> ``TlsTrustError``; altrimenti ``ApiError`` status 0."""
        if self.secure:
            tls = tls_problem(exc, self.base_url)
            if tls is not None:
                self.tls_issue = tls
                return tls
            if any(isinstance(e, ssl.SSLError) for e in _exc_chain(exc)):
                return ApiError(0, _trust.wrong_protocol_message(self.base_url))
        hint = ""
        if not self.secure and isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError)):
            hint = " Se il server usa HTTPS, scrivi l'indirizzo con https://."
        return ApiError(0, f"Server non raggiungibile: {exc}{hint}")

    @property
    def role(self) -> Role:
        return Role.parse(self.user.role) if self.user else Role.VIEWER

    @property
    def session_key(self) -> str:
        """Identificativo di presenza usato dal server nei partecipanti (``u<user_id>``)."""
        return f"u{self.user.id}" if self.user else ""

    def set_project(self, project_id: Optional[int], branch: Optional[str] = None) -> None:
        self.project_id = project_id
        self.branch = branch if project_id is not None else None

    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/ws?token={self.tokens.access_token}&project={self.project_id}" + (f"&branch={self.branch}" if self.branch else "")

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
                raise self._transport_error(exc) from exc
            if r.status_code != 200:
                self.tokens = None
                raise ApiError(401, "Sessione scaduta: effettua di nuovo il login")
            self._store(TokenPair(**r.json()))

    def _req(self, method: str, url: str, *, auth: bool = True, **kw) -> httpx.Response:
        if auth and self.tokens and time.monotonic() >= self._expires_at - 30:
            self._refresh()
        headers = {"Authorization": f"Bearer {self.tokens.access_token}"} if (auth and self.tokens) else {}
        branch, pid = kw.pop("branch", self.branch), self.project_id      # branch=None: sempre il principale (varianti *_for)
        if branch and pid is not None and url.startswith(f"/api/projects/{pid}/") \
                and not url.startswith(f"/api/projects/{pid}/branches"):
            kw["params"] = {**(kw.get("params") or {}), "branch": branch}      # le chiamate di progetto vanno al branch corrente
        try:
            r = self._http.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
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
        self.tls_issue = None
        try:
            return self._http.get("/api/health", timeout=4).status_code == 200
        except httpx.HTTPError as exc:
            if self.secure:
                self.tls_issue = tls_problem(exc, self.base_url)     # None se non e' un problema di certificato
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

    # ----------------------------------------------------------------- aggiornamento del client
    def client_update(self, current: str = "") -> UpdateInfo:
        return UpdateInfo(**self._req("GET", "/api/client/update", params={"current": current}).json())

    def download_client(self, dest: Path, progress: Optional[Callable[[int, int], None]] = None) -> None:
        """Scarica l'eseguibile pubblicato in streaming (senza tenerlo tutto in memoria, senza limite di tempo totale)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.tokens and time.monotonic() >= self._expires_at - 30:
            self._refresh()
        headers = {"Authorization": f"Bearer {self.tokens.access_token}"} if self.tokens else {}
        tmp = dest.with_name(dest.name + ".part")
        try:
            with self._http.stream("GET", "/api/client/download", headers=headers,
                                   timeout=httpx.Timeout(60.0, read=300.0)) as r:
                if r.status_code >= 400:
                    r.read()
                    try:
                        detail = r.json().get("detail", r.text)
                    except Exception:
                        detail = r.text
                    raise ApiError(r.status_code, str(detail))
                total, got = int(r.headers.get("content-length") or 0), 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, total)
            if total and got != total:
                raise ApiError(0, f"Download incompleto ({got} di {total} byte)")
            os.replace(tmp, dest)
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
        finally:
            tmp.unlink(missing_ok=True)

    def publish_client(self, file: Path, version: str, notes: str = "") -> UpdateInfo:
        with open(file, "rb") as f:
            r = self._req("POST", "/api/client/update", data={"version": version, "notes": notes},
                          files={"file": (Path(file).name, f, "application/octet-stream")})
        return UpdateInfo(**r.json())

    def notifications(self, unread_only: bool = False, limit: int = 100) -> list[NotificationOut]:
        return [NotificationOut(**n) for n in self._req("GET", "/api/notifications",
                                                         params={"unread_only": unread_only, "limit": limit}).json()]

    def notifications_unread(self) -> int:
        return int(self._req("GET", "/api/notifications/count").json().get("unread", 0))

    def mark_notifications_read(self, ids: Optional[list[int]] = None) -> int:
        return int(self._req("POST", "/api/notifications/read", json={"ids": ids}).json().get("unread", 0))

    def account_policy(self) -> AccountPolicy:
        return AccountPolicy(**self._req("GET", "/api/auth/policy").json())

    def set_account_policy(self, password_max_age_days: int) -> AccountPolicy:
        return AccountPolicy(**self._req("PUT", "/api/auth/policy",
                                         json={"password_max_age_days": password_max_age_days}).json())

    def unlock_user(self, uid: int) -> UserOut:
        return self.update_user(uid, UserUpdate(unlock=True))

    def delete_user(self, uid: int) -> None:
        self._req("DELETE", f"/api/users/{uid}")

    def update_user(self, uid: int, data: UserUpdate) -> UserOut:
        return UserOut(**self._req("PATCH", f"/api/users/{uid}", json=data.model_dump(exclude_none=True)).json())

    def projects(self) -> list[ProjectOut]:
        return [ProjectOut(**p) for p in self._req("GET", "/api/projects").json()]

    def create_project(self, data: ProjectCreate) -> ProjectOut:
        return ProjectOut(**self._req("POST", "/api/projects", json=data.model_dump(exclude_none=True)).json())

    def update_project(self, pid: int, data: ProjectUpdate) -> ProjectOut:
        return ProjectOut(**self._req("PATCH", f"/api/projects/{pid}", json=data.model_dump(exclude_none=True)).json())

    def delete_project(self, pid: int, confirm_name: str) -> ProjectDeleteOut:
        """Elimina il progetto, i suoi file e il suo audit (solo Admin). ``confirm_name`` deve essere il nome esatto."""
        return ProjectDeleteOut(**self._req("DELETE", f"/api/projects/{pid}", params={"confirm": confirm_name}).json())

    def export_project(self, pid: int, dest: Path) -> Path:
        """Scarica un archivio .zip del progetto (revisioni conservate + storico + audit CSV). Solo Admin."""
        r = self._req("GET", f"/api/projects/{pid}/export", branch=None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    def permissions(self, pid: int) -> list[PermissionOut]:
        return [PermissionOut(**p) for p in self._req("GET", f"/api/projects/{pid}/permissions", branch=None).json()]

    def set_permission(self, pid: int, user_id: int, role: Optional[str]) -> list[PermissionOut]:
        r = self._req("PUT", f"/api/projects/{pid}/permissions", json=PermissionIn(user_id=user_id, role=role).model_dump(),
                      branch=None)
        return [PermissionOut(**p) for p in r.json()]

    def init_master_for(self, pid: int, file: Path) -> MasterInfo:
        with open(file, "rb") as f:
            r = self._req("POST", f"/api/projects/{pid}/master/init", files={"file": (file.name, f, "application/octet-stream")},
                          branch=None)
        return MasterInfo(**r.json())

    def reimport_master_for(self, pid: int, file: Path, reason: str) -> MasterInfo:
        """Reimporta il file base (Master) di un progetto in corso: nuova revisione (Lead/Admin)."""
        with open(file, "rb") as f:
            r = self._req("POST", f"/api/projects/{pid}/master/reimport", data={"reason": reason},
                          files={"file": (file.name, f, "application/octet-stream")}, branch=None)
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

    # varianti con progetto esplicito: non toccano ``project_id`` (le usa la vista Admin, che lavora su un progetto
    # diverso da quello aperto in dashboard mentre altri thread usano lo stato condiviso)
    def history_for(self, pid: int) -> list[MasterInfo]:
        return [MasterInfo(**m) for m in self._req("GET", f"/api/projects/{pid}/master/history", branch=None).json()]

    def download_master_for(self, pid: int, dest: Path, revision: Optional[int] = None) -> int:
        r = self._req("GET", f"/api/projects/{pid}/master/download", params={"revision": revision} if revision else None,
                      branch=None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return int(r.headers["X-Revision"])

    def restore_for(self, pid: int, revision: int, reason: str) -> MasterInfo:
        return MasterInfo(**self._req("POST", f"/api/projects/{pid}/master/restore",
                                      json=RestoreIn(revision=revision, reason=reason).model_dump(), branch=None).json())

    def report_revision(self, revision: Optional[int] = None) -> None:
        """Heartbeat di presenza (+ revisione scaricata): rende l'utente 'attivo' per il quorum."""
        self._req("POST", self._p("/heartbeat"), params={"revision": revision} if revision else None)

    def developers(self) -> list[DeveloperOut]:
        return [DeveloperOut(**d) for d in self._req("GET", self._p("/developers")).json()]

    def presence(self) -> list[PresenceOut]:
        """Stato dei colleghi (Master/branch, revisione di lavoro, lock). Con server vecchi ripiega su /developers."""
        try:
            return [PresenceOut(**d) for d in self._req("GET", self._p("/presence"), branch=None).json()]
        except ApiError as e:
            if e.status not in (404, 405):
                raise
        out = []
        for d in self.developers():
            out.append(PresenceOut(username=d.developer, role=d.role, online=d.connected, contexts=[
                PresenceContext(branch="", branch_name="Master", revision=d.checked_out_revision, online=d.connected)]))
        return out

    # ----------------------------------------------------------------- proposte
    def submit_proposal(self, file: Path, base_revision: int, message: str = "") -> ProposalDetail:
        with open(file, "rb") as f:
            r = self._req("POST", self._p("/proposals"), data={"base_revision": base_revision, "message": message},
                          files={"file": (file.name, f, "application/xml")})
        return ProposalDetail(**r.json())

    def dry_run(self, file: Path, base_revision: int) -> PreviewOut:
        """Anteprima di una richiesta di merge: quante modifiche e quali conflitti, senza inviare nulla."""
        with open(file, "rb") as f:
            r = self._req("POST", self._p("/proposals/dry-run"), data={"base_revision": base_revision},
                          files={"file": (Path(file).name, f, "application/xml")})
        return PreviewOut(**r.json())

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

    # varianti con progetto / branch espliciti (risposta rapida dalle notifiche, anche per un altro progetto)
    def _prop_url(self, project_id: int, pid: str, tail: str = "") -> str:
        return f"/api/projects/{project_id}/proposals/{pid}{tail}"

    def proposal_for(self, project_id: int, branch: Optional[str], pid: str) -> ProposalDetail:
        r = self._req("GET", self._prop_url(project_id, pid), branch=None, params={"branch": branch} if branch else None)
        return ProposalDetail(**r.json())

    def decide_for(self, project_id: int, branch: Optional[str], pid: str, approve: bool, comment: str = "") -> ProposalDetail:
        r = self._req("POST", self._prop_url(project_id, pid, "/approve" if approve else "/reject"), branch=None,
                      params={"branch": branch} if branch else None, json=DecisionIn(comment=comment).model_dump())
        return ProposalDetail(**r.json())

    def cancel(self, pid: str) -> ProposalDetail:
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/cancel")).json())

    def force_merge(self, pid: str, comment: str) -> ProposalDetail:
        return ProposalDetail(**self._req("POST", self._p(f"/proposals/{pid}/force-merge"),
                                          json=DecisionIn(comment=comment).model_dump()).json())

    # ------------------------------------------------------------------ discussione sulle richieste
    def comments(self, prop_id: str) -> list[CommentOut]:
        return [CommentOut(**c) for c in self._req("GET", self._p(f"/proposals/{prop_id}/comments")).json()]

    def add_comment(self, prop_id: str, text: str, reply_to: Optional[int] = None,
                    anchor: Optional[str] = None) -> CommentOut:
        body = CommentIn(text=text, reply_to=reply_to, anchor=anchor or None)
        return CommentOut(**self._req("POST", self._p(f"/proposals/{prop_id}/comments"), json=body.model_dump()).json())

    def edit_comment(self, comment_id: int, text: str) -> CommentOut:
        return CommentOut(**self._req("PATCH", self._p(f"/comments/{comment_id}"),
                                      json=CommentEdit(text=text).model_dump()).json())

    def delete_comment(self, comment_id: int) -> CommentOut:
        return CommentOut(**self._req("DELETE", self._p(f"/comments/{comment_id}")).json())

    # ------------------------------------------- confronto / etichette / blocco delle revisioni, lock, check-in
    def compare_revisions(self, a: int, b: int) -> CompareOut:
        """Differenze dalla rev ``a`` alla rev ``b`` (entrambe conservate; non per i file binari)."""
        return CompareOut(**self._req("GET", self._p("/master/compare"), params={"from": a, "to": b}).json())

    def compare_revisions_for(self, pid: int, a: int, b: int) -> CompareOut:
        return CompareOut(**self._req("GET", f"/api/projects/{pid}/master/compare", params={"from": a, "to": b},
                                      branch=None).json())

    def patch_revision(self, revision: int, label: Optional[str] = None, locked: Optional[bool] = None) -> MasterInfo:
        """Etichetta e/o blocca una revisione (Lead/Admin; sbloccare: solo Admin)."""
        body = RevisionPatchIn(label=label, locked=locked).model_dump(exclude_none=True)
        return MasterInfo(**self._req("PATCH", self._p(f"/master/revisions/{revision}"), json=body).json())

    def patch_revision_for(self, pid: int, revision: int, label: Optional[str] = None,
                           locked: Optional[bool] = None) -> MasterInfo:
        body = RevisionPatchIn(label=label, locked=locked).model_dump(exclude_none=True)
        return MasterInfo(**self._req("PATCH", f"/api/projects/{pid}/master/revisions/{revision}", json=body,
                                      branch=None).json())

    def lock_state(self) -> LockOut:
        return LockOut(**self._req("GET", self._p("/lock")).json())

    def lock_acquire(self, note: str = "") -> LockOut:
        """«Prendi in carico» il Master (409 se e' gia' di un altro)."""
        return LockOut(**self._req("POST", self._p("/lock"), json=LockIn(note=note).model_dump()).json())

    def lock_renew(self) -> LockOut:
        return LockOut(**self._req("POST", self._p("/lock/renew")).json())

    def lock_release(self, reason: str = "") -> LockOut:
        """«Rilascia» (holder) o «Forza rilascio» (Admin, ``reason`` obbligatorio)."""
        return LockOut(**self._req("DELETE", self._p("/lock"), params={"reason": reason} if reason else None).json())

    def checkin_master(self, file: Path, message: str) -> MasterInfo:
        """Check-in diretto di una nuova revisione di un Master binario (serve la presa in carico attiva)."""
        with open(file, "rb") as f:
            r = self._req("POST", self._p("/master/checkin"), data={"message": message},
                          files={"file": (file.name, f, "application/octet-stream")})
        return MasterInfo(**r.json())

    # ------------------------------------------------------------------ branch
    def _bp(self, pid: Optional[int]) -> int:
        """Progetto delle chiamate sui branch: ``pid`` esplicito (console Admin) oppure quello aperto."""
        pid = self.project_id if pid is None else pid
        if pid is None:
            raise ApiError(0, "Nessun progetto selezionato")
        return pid

    def branches(self, pid: Optional[int] = None) -> list[BranchOut]:
        return [BranchOut(**b) for b in self._req("GET", f"/api/projects/{self._bp(pid)}/branches").json()]

    def create_branch(self, name: str, kind: str = "personal", from_revision: Optional[int] = None,
                      pid: Optional[int] = None) -> BranchOut:
        body = BranchCreate(name=name, kind=kind, from_revision=from_revision).model_dump(exclude_none=True)
        return BranchOut(**self._req("POST", f"/api/projects/{self._bp(pid)}/branches", json=body).json())

    def branch_commit(self, slug: str, file: Path, message: str) -> MasterInfo:
        """Salvataggio diretto di una nuova revisione su un branch personale (proprietario o Admin)."""
        with open(file, "rb") as f:
            r = self._req("POST", f"/api/projects/{self.project_id}/branches/{slug}/commit", data={"message": message},
                          files={"file": (file.name, f, "application/octet-stream")})
        return MasterInfo(**r.json())

    def branch_save(self, slug: str, file: Path, message: str, base_revision: Optional[int] = None) -> ProposalDetail:
        """«Salva su branch» in un branch di TEAM: unione 3-way con la testa del branch e consenso automatico dell'autore."""
        data = {"message": message, **({"base_revision": base_revision} if base_revision else {})}
        with open(file, "rb") as f:
            r = self._req("POST", f"/api/projects/{self.project_id}/branches/{slug}/save", data=data,
                          files={"file": (Path(file).name, f, "application/xml")})
        return ProposalDetail(**r.json())

    def branch_sync(self, slug: str) -> ProposalDetail:
        """«Aggiorna dal principale»: porta nel branch le novita' del principale (richiesta di merge NEL branch)."""
        return ProposalDetail(**self._req("POST", f"/api/projects/{self.project_id}/branches/{slug}/sync").json())

    def branch_merge(self, slug: str, message: str = "", file: Optional[Path] = None) -> ProposalDetail:
        """«Unisci nel principale»: richiesta di merge del branch NEL principale (consenso normale). Con ``file`` si
        invia quella versione di lavoro al posto della testa del branch."""
        url = f"/api/projects/{self.project_id}/branches/{slug}/merge"
        if file is None:
            return ProposalDetail(**self._req("POST", url, data={"message": message}).json())
        with open(file, "rb") as f:
            return ProposalDetail(**self._req("POST", url, data={"message": message},
                                              files={"file": (Path(file).name, f, "application/xml")}).json())

    def branch_dry_run(self, slug: str, mode: str = "merge", file: Optional[Path] = None) -> PreviewOut:
        """Anteprima senza effetti: ``mode`` = merge («Unisci nel principale») o sync («Aggiorna dal principale»)."""
        url = f"/api/projects/{self.project_id}/branches/{slug}/dry-run"
        if file is None:
            return PreviewOut(**self._req("POST", url, data={"mode": mode}).json())
        with open(file, "rb") as f:
            return PreviewOut(**self._req("POST", url, data={"mode": mode},
                                          files={"file": (Path(file).name, f, "application/xml")}).json())

    def close_branch(self, slug: str, pid: Optional[int] = None) -> BranchOut:
        return BranchOut(**self._req("POST", f"/api/projects/{self._bp(pid)}/branches/{slug}/close").json())

    def delete_branch(self, slug: str, pid: Optional[int] = None) -> None:
        self._req("DELETE", f"/api/projects/{self._bp(pid)}/branches/{slug}")
