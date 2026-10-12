"""Modelli del modulo auth: ruoli, principal autenticato, errori. (Le tabelle sono in ``server/db/models.py``.)"""
from __future__ import annotations

from dataclasses import dataclass

from common.roles import ROLE_KEYS, ROLE_LABELS, Role  # noqa: F401  (ri-esportati)


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class Principal:
    """Identita' autenticata di una richiesta (il ruolo e' sempre riletto dal DB, non dal token)."""
    user_id: int
    username: str
    display_name: str
    role: Role                      # ruolo massimo dell'account
    session_id: str
    must_change_password: bool = False
    ip: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role >= Role.ADMIN


@dataclass(frozen=True)
class ProjectAccess:
    """Principal + progetto + ruolo effettivo su quel progetto."""
    principal: Principal
    project_id: int
    slug: str
    name: str
    role: Role
    require_lead_approval: bool
