"""Schema del database: utenti, ruoli, progetti, permessi per progetto, sessioni token, audit log."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (Boolean, DDL, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
                        event)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.roles import ROLE_DESCRIPTIONS, ROLE_KEYS, Role
from server.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RoleRow(Base):
    """Tabella ``roles``: i 4 ruoli RBAC (seed automatico, ``rank`` = Role.value)."""
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)          # Admin | Lead_Engineer | Developer | Viewer
    rank: Mapped[int] = mapped_column(Integer, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")

    @property
    def role(self) -> Role:
        return Role(self.rank)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)       # sempre minuscolo
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(256), default="")
    password_hash: Mapped[str] = mapped_column(String(128))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))          # ruolo massimo dell'account
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped[RoleRow] = relationship(lazy="joined")
    permissions: Mapped[list["ProjectPermission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", foreign_keys="ProjectPermission.user_id")


class Project(Base):
    """Un progetto PLC (un Master.L5X con il suo storico revisioni)."""
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # nome cartella dati
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    require_lead_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")

    permissions: Mapped[list["ProjectPermission"]] = relationship(back_populates="project",
                                                                  cascade="all, delete-orphan")


class ProjectPermission(Base):
    """Ruolo di un utente su un singolo progetto.

    Ruolo effettivo = min(ruolo account, ruolo sul progetto); gli Admin hanno accesso a tutto.
    """
    __tablename__ = "project_permissions"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_user_project"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="permissions", foreign_keys=[user_id])
    project: Mapped[Project] = relationship(back_populates="permissions")
    role: Mapped[RoleRow] = relationship(lazy="joined")


class AuthSession(Base):
    """Sessione di un client (un refresh token): revocabile dall'utente o dall'Admin."""
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_jti: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    client: Mapped[str] = mapped_column(String(200), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")

    user: Mapped[User] = relationship()


class AuditLog(Base):
    """Registro eventi **append-only** con catena di hash (vedi ``server/db/audit.py``)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)              # ISO-8601 UTC
    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    details: Mapped[str] = mapped_column(Text, default="{}")             # JSON canonico
    ip: Mapped[str] = mapped_column(String(64), default="")
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


# ---- immutabilita' dell'audit log: trigger SQLite + guardia ORM -----------------------------------
event.listen(AuditLog.__table__, "after_create", DDL(
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log e'' immutabile'); END;").execute_if(dialect="sqlite"))
event.listen(AuditLog.__table__, "after_create", DDL(
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log e'' immutabile'); END;").execute_if(dialect="sqlite"))


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_guard(*_a):                                                   # noqa: ANN002
    raise PermissionError("audit_log e' append-only")


def seed_roles(session) -> None:                                         # noqa: ANN001
    have = {r.rank for r in session.query(RoleRow).all()}
    for role in Role:
        if int(role) not in have:
            session.add(RoleRow(name=ROLE_KEYS[role], rank=int(role), description=ROLE_DESCRIPTIONS[role]))
    session.flush()
