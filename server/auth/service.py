"""Logica di autenticazione e gestione utenti/sessioni."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from common.roles import ROLE_DESCRIPTIONS, ROLE_KEYS, Role
from common.schemas import AccountPolicy, AuthSessionOut, RoleOut, TokenPair, UserCreate, UserOut, UserUpdate
from server.auth.models import AuthError, Principal
from server.auth.security import (LoginThrottle, TokenError, TokenService, generate_password,
                                  hash_password, validate_password_policy, verify_password)
from server.config import Settings
from server.db.audit import AuditService
from server.db.base import Database
from server.db.models import AuthSession, RoleRow, ServerSetting, User, seed_roles


CONSOLE_PREFIX = "console-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite restituisce datetime naive: si assume UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def user_out(u: User, max_age_days: int = 0) -> UserOut:
    r = Role(u.role.rank)
    changed = aware(u.password_changed_at) or aware(u.created_at)
    locked = aware(u.locked_until)
    return UserOut(locked_until=locked if locked and locked > _now() else None,
                   password_changed_at=aware(u.password_changed_at),
                   password_expires_at=changed + timedelta(days=max_age_days) if (max_age_days and changed) else None,
                   id=u.id, username=u.username, display_name=u.display_name, email=u.email, role=r.key,
                   role_label=r.label, is_active=u.is_active, must_change_password=u.must_change_password,
                   created_at=aware(u.created_at), last_login_at=aware(u.last_login_at))


class AuthService:
    def __init__(self, db: Database, tokens: TokenService, audit: AuditService, cfg: Settings):
        self.db, self.tokens, self.audit, self.cfg = db, tokens, audit, cfg
        self.throttle = LoginThrottle(cfg.max_login_failures, cfg.lockout_seconds)
        self._dummy_hash = hash_password("dummy-password-1", cfg.bcrypt_rounds)
        self._max_age = 0                                  # scadenza password in giorni (0 = mai); vedi ``policy``

    # ------------------------------------------------------------------ criteri account
    def policy(self) -> AccountPolicy:
        with self.db.scope() as s:
            row = s.get(ServerSetting, "password_max_age_days")
            try:
                self._max_age = int(row.value) if row else 0
            except ValueError:
                self._max_age = 0
        return AccountPolicy(password_max_age_days=self._max_age)

    def set_policy(self, actor: Principal, data: AccountPolicy) -> AccountPolicy:
        with self.db.scope() as s:
            row = s.get(ServerSetting, "password_max_age_days")
            if row is None:
                s.add(ServerSetting(key="password_max_age_days", value=str(data.password_max_age_days)))
            else:
                row.value = str(data.password_max_age_days)
        self._max_age = data.password_max_age_days
        self.audit.record("account_policy_changed", actor.username, ip=actor.ip,
                          details={"password_max_age_days": data.password_max_age_days})
        return data

    def _uo(self, u: User) -> UserOut:
        return user_out(u, self._max_age)

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> Optional[str]:
        """Crea i ruoli e, al primo avvio, l'utente admin. Ritorna la password generata (se generata)."""
        with self.db.scope() as s:
            seed_roles(s)
            if s.scalar(select(func.count(User.id))) > 0:
                return None
            pw = self.cfg.admin_password
            generated = pw is None
            pw = pw or generate_password()
            validate_password_policy(pw)
            rid = s.scalar(select(RoleRow.id).where(RoleRow.rank == int(Role.ADMIN)))
            s.add(User(password_changed_at=_now(), username=self.cfg.admin_username.lower(), display_name="Project Administrator",
                       password_hash=hash_password(pw, self.cfg.bcrypt_rounds), role_id=rid,
                       must_change_password=generated))
        self.audit.record("user_created", "system", target=self.cfg.admin_username, details={"role": "Admin", "bootstrap": True})
        return pw if generated else None

    # ------------------------------------------------------------------ login / token
    def _pair(self, user: User, sid: str, jti: str) -> TokenPair:
        role = Role(user.role.rank)
        return TokenPair(access_token=self.tokens.access(user.id, sid, user.username, role.key),
                         refresh_token=self.tokens.refresh(user.id, sid, jti),
                         expires_in=int(self.tokens.access_ttl.total_seconds()), session_id=sid,
                         user=self._uo(user))

    def login(self, username: str, password: str, ip: str = "", client: str = "") -> TokenPair:
        key = username.strip().lower()
        wait = self.throttle.remaining(key)
        if wait > 0:
            self.audit.record("login_blocked", key, ip=ip, details={"retry_after_s": int(wait)})
            raise AuthError(429, f"Troppi tentativi falliti: riprova tra {int(wait) + 1} secondi")
        with self.db.scope() as s:
            user = s.scalar(select(User).where(User.username == key))
            self.policy()
            until = aware(user.locked_until) if user else None
            if until and until > _now():                       # blocco persistente (sopravvive al riavvio del server)
                secs = int((until - _now()).total_seconds())
                self.audit.record("login_blocked", key, ip=ip, details={"retry_after_s": secs, "persistent": True})
                raise AuthError(429, f"Account bloccato per troppi tentativi falliti: riprova tra {secs + 1} secondi "
                                     "o chiedi a un amministratore di sbloccarlo")
            ok = verify_password(password, user.password_hash if user else self._dummy_hash)
            if not (user and ok and user.is_active):
                self.throttle.fail(key)
                locked = False
                if user:
                    user.failed_attempts = (user.failed_attempts or 0) + 1
                    if user.failed_attempts >= self.cfg.max_login_failures:
                        user.locked_until = _now() + timedelta(seconds=self.cfg.lockout_seconds)
                        user.failed_attempts = 0
                        locked = True
                    s.commit()
                self.audit.record("login_failed", key, ip=ip)
                if locked:
                    self.audit.record("account_locked", key, ip=ip, details={"seconds": int(self.cfg.lockout_seconds)})
                raise AuthError(401, "Credenziali non valide")
            self.throttle.success(key)
            user.failed_attempts, user.locked_until = 0, None
            if self._max_age and not user.must_change_password:
                changed = aware(user.password_changed_at) or aware(user.created_at)
                if changed and _now() - changed > timedelta(days=self._max_age):
                    user.must_change_password = True           # password scaduta: cambio obbligatorio al primo accesso
                    self.audit.record("password_expired", key, ip=ip, details={"max_age_days": self._max_age})
            sid, jti = uuid.uuid4().hex, uuid.uuid4().hex
            now = _now()
            user.last_login_at = now
            s.add(AuthSession(id=sid, user_id=user.id, refresh_jti=jti, created_at=now, last_seen=now,
                              expires_at=now + self.tokens.refresh_ttl, client=client[:200], ip=ip))
            s.flush()
            pair = self._pair(user, sid, jti)
        self.audit.record("login", key, ip=ip, details={"client": client[:80]})
        return pair

    def console_session(self) -> TokenPair:
        """Sessione amministrativa per la console locale del server (GUI), senza password.

        L'accesso fisico/di processo al server equivale a quello dell'amministratore di sistema (come l'applicazione
        di amministrazione di Siemens Project Server): la sessione e' legata al primo amministratore attivo, ha
        identificativo ``console-...`` (non falsificabile: i token sono firmati dal server) e viene tracciata nell'audit.
        """
        with self.db.scope() as s:
            user = s.scalar(select(User).join(RoleRow, RoleRow.id == User.role_id)
                            .where(RoleRow.rank == int(Role.ADMIN), User.is_active.is_(True)).order_by(User.id))
            if user is None:
                raise AuthError(409, "Nessun amministratore attivo")
            sid, jti = CONSOLE_PREFIX + uuid.uuid4().hex, uuid.uuid4().hex
            now = _now()
            s.add(AuthSession(id=sid, user_id=user.id, refresh_jti=jti, created_at=now, last_seen=now,
                              expires_at=now + self.tokens.refresh_ttl, client="Server console", ip="local"))
            s.flush()
            pair, uname = self._pair(user, sid, jti), user.username
        self.audit.record("console_session", uname, ip="local")
        return pair

    def refresh(self, refresh_token: str, ip: str = "") -> TokenPair:
        try:
            claims = self.tokens.decode(refresh_token, "refresh")
        except TokenError as exc:
            raise AuthError(401, str(exc)) from exc
        with self.db.scope() as s:
            sess = s.get(AuthSession, claims["sid"])
            user = s.get(User, int(claims["sub"]))
            if not sess or sess.revoked or not user or not user.is_active or aware(sess.expires_at) < _now():
                raise AuthError(401, "Sessione terminata: effettuare nuovamente il login")
            if sess.refresh_jti != claims["jti"]:         # refresh token gia' usato: possibile furto
                sess.revoked = True
                s.commit()
                self.audit.record("token_reuse_detected", user.username, ip=ip, target=sess.id)
                raise AuthError(401, "Refresh token riutilizzato: sessione revocata")
            jti = uuid.uuid4().hex
            sess.refresh_jti, sess.last_seen = jti, _now()
            sess.expires_at = _now() + self.tokens.refresh_ttl
            s.flush()
            return self._pair(user, sess.id, jti)

    def principal_from_access(self, token: str, ip: str = "") -> Principal:
        try:
            claims = self.tokens.decode(token, "access")
        except TokenError as exc:
            raise AuthError(401, str(exc)) from exc
        with self.db.scope() as s:
            sess = s.get(AuthSession, claims["sid"])
            user = s.get(User, int(claims["sub"]))
            if not sess or sess.revoked or not user or not user.is_active or aware(sess.expires_at) < _now():
                raise AuthError(401, "Sessione terminata: effettuare nuovamente il login")
            if (_now() - aware(sess.last_seen)).total_seconds() > 60:
                sess.last_seen = _now()
            # le sessioni "console" (GUI locale del server) non sono soggette al cambio password obbligatorio
            must_change = user.must_change_password and not sess.id.startswith(CONSOLE_PREFIX)
            return Principal(user.id, user.username, user.display_name, Role(user.role.rank), sess.id,
                             must_change, ip)

    def logout(self, p: Principal) -> None:
        with self.db.scope() as s:
            sess = s.get(AuthSession, p.session_id)
            if sess:
                sess.revoked = True
        self.audit.record("logout", p.username, ip=p.ip)

    # ------------------------------------------------------------------ sessioni
    def list_sessions(self, p: Principal, all_users: bool = False) -> list[AuthSessionOut]:
        with self.db.scope() as s:
            q = select(AuthSession, User.username).join(User, User.id == AuthSession.user_id) \
                .where(AuthSession.revoked.is_(False)).order_by(AuthSession.last_seen.desc())
            if not (all_users and p.is_admin):
                q = q.where(AuthSession.user_id == p.user_id)
            return [AuthSessionOut(id=a.id, user_id=a.user_id, username=name, created_at=aware(a.created_at),
                                   last_seen=aware(a.last_seen), expires_at=aware(a.expires_at),
                                   revoked=a.revoked, client=a.client, ip=a.ip, current=a.id == p.session_id)
                    for a, name in s.execute(q).all() if aware(a.expires_at) > _now()]

    def revoke_session(self, p: Principal, sid: str) -> None:
        with self.db.scope() as s:
            sess = s.get(AuthSession, sid)
            if not sess or (sess.user_id != p.user_id and not p.is_admin):
                raise AuthError(404, "Sessione inesistente")
            sess.revoked = True
        self.audit.record("session_revoked", p.username, target=sid, ip=p.ip)

    def _revoke_all(self, s, user_id: int) -> None:                    # noqa: ANN001
        for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == user_id)):
            sess.revoked = True

    # ------------------------------------------------------------------ utenti
    @staticmethod
    def roles() -> list[RoleOut]:
        return [RoleOut(key=r.key, label=r.label, rank=int(r), description=ROLE_DESCRIPTIONS[r])
                for r in sorted(Role, reverse=True)]

    def list_users(self) -> list[UserOut]:
        with self.db.scope() as s:
            self.policy()
            return [self._uo(u) for u in s.scalars(select(User).order_by(User.username))]

    def _role_id(self, s, role: str) -> int:                           # noqa: ANN001
        try:
            r = Role.parse(role)
        except ValueError as exc:
            raise AuthError(422, str(exc)) from exc
        return s.scalar(select(RoleRow.id).where(RoleRow.rank == int(r)))

    def create_user(self, actor: Principal, data: UserCreate) -> UserOut:
        try:
            validate_password_policy(data.password)
        except ValueError as exc:
            raise AuthError(422, str(exc)) from exc
        name = data.username.strip().lower()
        with self.db.scope() as s:
            if s.scalar(select(User.id).where(User.username == name)):
                raise AuthError(409, f"Utente '{name}' gia' esistente")
            u = User(username=name, display_name=data.display_name.strip(), email=data.email.strip(),
                     password_hash=hash_password(data.password, self.cfg.bcrypt_rounds),
                     role_id=self._role_id(s, data.role), must_change_password=data.must_change_password,
                     password_changed_at=_now())
            s.add(u)
            s.flush()
            s.refresh(u)
            out = self._uo(u)
        self.audit.record("user_created", actor.username, target=name, ip=actor.ip, details={"role": out.role})
        return out

    def update_user(self, actor: Principal, uid: int, data: UserUpdate) -> UserOut:
        changed: dict = {}
        with self.db.scope() as s:
            u = s.get(User, uid)
            if not u:
                raise AuthError(404, "Utente inesistente")
            admins = s.scalar(select(func.count(User.id)).join(RoleRow, RoleRow.id == User.role_id)
                              .where(RoleRow.rank == int(Role.ADMIN), User.is_active.is_(True)))
            is_admin = u.role.rank == int(Role.ADMIN) and u.is_active
            if data.display_name is not None:
                u.display_name, changed["display_name"] = data.display_name.strip(), True
            if data.email is not None:
                u.email, changed["email"] = data.email.strip(), True
            if data.role is not None:
                rid = self._role_id(s, data.role)
                new_rank = s.get(RoleRow, rid).rank
                if is_admin and new_rank != int(Role.ADMIN) and admins <= 1:
                    raise AuthError(409, "Non si puo' declassare l'ultimo amministratore attivo")
                if u.id == actor.user_id and new_rank != int(Role.ADMIN):
                    raise AuthError(409, "Non puoi modificare il tuo stesso ruolo")
                u.role_id = rid
                s.flush()
                s.refresh(u)
                changed["role"] = Role(new_rank).key
            if data.is_active is not None and data.is_active != u.is_active:
                if not data.is_active and (u.id == actor.user_id or (is_admin and admins <= 1)):
                    raise AuthError(409, "Non si puo' disattivare te stesso o l'ultimo amministratore")
                u.is_active = data.is_active
                changed["is_active"] = data.is_active
                if not data.is_active:
                    self._revoke_all(s, u.id)
            if data.new_password:
                try:
                    validate_password_policy(data.new_password)
                except ValueError as exc:
                    raise AuthError(422, str(exc)) from exc
                u.password_hash = hash_password(data.new_password, self.cfg.bcrypt_rounds)
                u.must_change_password = True
                u.password_changed_at = _now()
                self._revoke_all(s, u.id)
                changed["password_reset"] = True
            if data.must_change_password is not None:
                u.must_change_password = data.must_change_password
            if data.unlock:
                u.failed_attempts, u.locked_until = 0, None
                self.throttle.success(u.username)
                changed["unlocked"] = True
            s.flush()
            s.refresh(u)
            out, uname = self._uo(u), u.username
        self.audit.record("user_updated", actor.username, target=uname, ip=actor.ip, details=changed)
        return out

    def delete_user(self, actor: Principal, uid: int) -> None:
        """Elimina definitivamente un utente (solo Admin): sessioni e permessi di progetto spariscono con lui. Non si puo'
        eliminare se stessi ne' l'ultimo amministratore attivo. Cronologia eventi, commenti e branch gia' creati restano
        (portano solo il nome utente); l'azione e' registrata come ``user_deleted``."""
        with self.db.scope() as s:
            u = s.get(User, uid)
            if not u:
                raise AuthError(404, "Utente inesistente")
            if u.id == actor.user_id:
                raise AuthError(409, "Non puoi eliminare te stesso")
            admins = s.scalar(select(func.count(User.id)).join(RoleRow, RoleRow.id == User.role_id)
                              .where(RoleRow.rank == int(Role.ADMIN), User.is_active.is_(True)))
            if u.role.rank == int(Role.ADMIN) and u.is_active and admins <= 1:
                raise AuthError(409, "Non si puo' eliminare l'ultimo amministratore attivo")
            name, role = u.username, Role(u.role.rank).key
            n_perm = len(u.permissions)
            for sess in list(s.scalars(select(AuthSession).where(AuthSession.user_id == u.id))):
                s.delete(sess)
            s.delete(u)                                           # i permessi di progetto vanno via in cascata
        self.audit.record("user_deleted", actor.username, target=name, ip=actor.ip,
                          details={"role": role, "permissions_removed": n_perm})

    def change_password(self, p: Principal, old: str, new: str) -> None:
        try:
            validate_password_policy(new)
        except ValueError as exc:
            raise AuthError(422, str(exc)) from exc
        if old == new:
            raise AuthError(422, "La nuova password deve essere diversa dalla precedente")
        with self.db.scope() as s:
            u = s.get(User, p.user_id)
            if not verify_password(old, u.password_hash):
                raise AuthError(403, "Password attuale errata")
            u.password_hash = hash_password(new, self.cfg.bcrypt_rounds)
            u.must_change_password = False
            u.password_changed_at = _now()
            for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == u.id)):
                if sess.id != p.session_id:
                    sess.revoked = True                      # le altre sessioni vengono chiuse
        self.audit.record("password_changed", p.username, ip=p.ip)

    def me(self, p: Principal) -> UserOut:
        with self.db.scope() as s:
            self.policy()
            return self._uo(s.get(User, p.user_id))
