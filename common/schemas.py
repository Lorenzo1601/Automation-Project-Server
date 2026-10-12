"""Schemi condivisi tra server e client (contratto REST/WebSocket)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ProposalState(str, Enum):
    AWAITING_CONSENT = "awaiting_consent"  # In attesa di consenso
    CONFLICT = "conflict"                  # Conflitto rilevato
    APPROVED = "approved"                  # Approvato (transitorio: subito dopo -> MERGED)
    MERGED = "merged"                      # Merged su Master
    REJECTED = "rejected"                  # Rifiutato / richieste modifiche
    CANCELLED = "cancelled"                # Annullato dall'autore o dal server


STATE_LABELS = {
    ProposalState.AWAITING_CONSENT: "In attesa di consenso",
    ProposalState.CONFLICT: "Conflitto rilevato",
    ProposalState.APPROVED: "Approvato",
    ProposalState.MERGED: "Merged",
    ProposalState.REJECTED: "Rifiutato / modifiche richieste",
    ProposalState.CANCELLED: "Annullato",
}

ACTIVE_STATES = {ProposalState.AWAITING_CONSENT, ProposalState.CONFLICT, ProposalState.APPROVED}


# ------------------------------------------------------- auth / utenti / ruoli
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str


class RoleOut(BaseModel):
    key: str                       # Admin | Lead_Engineer | Developer | Viewer
    label: str
    rank: int
    description: str = ""


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    email: str = ""
    role: str                      # chiave ruolo account (massimo)
    role_label: str = ""
    is_active: bool = True
    must_change_password: bool = False
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int                # secondi di validita' dell'access token
    session_id: str
    user: UserOut


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    email: str = ""
    password: str
    role: str = "Developer"
    must_change_password: bool = True


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    new_password: Optional[str] = None          # reset da parte dell'Admin
    must_change_password: Optional[bool] = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class AuthSessionOut(BaseModel):
    id: str
    user_id: int
    username: str
    created_at: datetime
    last_seen: datetime
    expires_at: datetime
    revoked: bool
    client: str = ""
    ip: str = ""
    current: bool = False


class ProjectOut(BaseModel):
    id: int
    slug: str
    name: str
    description: str = ""
    require_lead_approval: bool = True
    archived: bool = False
    my_role: Optional[str] = None
    head_revision: Optional[int] = None         # None = Master non ancora inizializzato
    master_kind: Optional[str] = None           # tipo/estensione del Master (None finche' non e' inizializzato)
    master_ext: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    description: str = ""
    require_lead_approval: bool = True


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    require_lead_approval: Optional[bool] = None
    archived: Optional[bool] = None


class PermissionOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    account_role: str                           # ruolo massimo dell'account
    role: Optional[str] = None                  # ruolo assegnato sul progetto (None = nessun accesso)
    effective_role: Optional[str] = None        # min(account, progetto)


class PermissionIn(BaseModel):
    user_id: int
    role: Optional[str] = None                  # None = revoca accesso


class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor: str
    action: str
    project_id: Optional[int] = None
    target: str = ""
    details: dict = {}
    ip: str = ""
    hash: str = ""


class AuditVerifyOut(BaseModel):
    ok: bool
    checked: int
    first_bad_id: Optional[int] = None


# ---------------------------------------------------------------- presenza
class DeveloperOut(BaseModel):
    session_id: str
    developer: str
    role: str = ""
    connected: bool
    checked_out_revision: int
    last_seen: datetime


class MasterInfo(BaseModel):
    revision: int
    sha256: str
    updated_at: datetime
    updated_by: str
    message: str = ""
    proposal_id: Optional[str] = None
    kind: str = "l5x"                  # l5x | l5k | text | binary (common.filekinds)
    ext: str = ".L5X"                  # estensione del Master (fissata dal primo file importato)


# ----------------------------------------------------------- diff / merge
class ChangeOut(BaseModel):
    path: str                      # es. Program[Main]/Routine[R01]/Rung#3
    title: str                     # etichetta breve per la GUI
    label: str                     # tag XML (Tag, Rung, DataType, @attr ...)
    kind: Literal["added", "modified", "deleted"]
    origin: Literal["proposal", "master", "both"]
    base_text: str = ""
    new_text: str = ""


class ConflictOut(BaseModel):
    id: str
    path: str
    title: str
    label: str
    kind: str                      # modify/modify | modify/delete | add/add
    sequence: bool                 # True = blocco di Rung/Line, False = elemento intero
    base_text: str = ""
    mine_text: str = ""
    theirs_text: str = ""
    resolved: bool = False
    choice: Optional[str] = None


class ResolutionIn(BaseModel):
    choice: Literal["mine", "theirs", "both", "custom"]
    # custom: elemento atomico -> [xml]; sequenza -> lista di testi (uno per Rung/Line)
    custom: Optional[list[str]] = None


class ResolutionsIn(BaseModel):
    resolutions: dict[str, ResolutionIn]


class DecisionIn(BaseModel):
    comment: str = ""


class RestoreIn(BaseModel):
    revision: int
    reason: str = Field(min_length=3)


# ------------------------------------------------------------- proposte
class ParticipantOut(BaseModel):
    session_id: str
    developer: str
    role: str = ""
    is_author: bool = False
    decision: Literal["pending", "approve", "reject"] = "pending"
    comment: str = ""
    online: bool = True


class ProposalSummary(BaseModel):
    id: str
    author: str
    author_session: str
    base_revision: int
    head_revision: int             # revisione di Master contro cui e' stata calcolata l'analisi
    state: ProposalState
    state_label: str
    message: str = ""
    created_at: datetime
    updated_at: datetime
    n_changes: int = 0
    n_upstream: int = 0
    n_conflicts: int = 0
    n_unresolved: int = 0
    touched: list[str] = []
    participants: list[ParticipantOut] = []
    digest: str = ""
    warnings: list[str] = []
    result_revision: Optional[int] = None   # valorizzato quando MERGED
    needs_lead_approval: bool = False       # serve il Consenso Finale di un Lead/Admin
    lead_approved_by: Optional[str] = None
    forced_by: Optional[str] = None         # Admin che ha forzato il merge
    author_role: str = ""


class ProposalDetail(ProposalSummary):
    changes: list[ChangeOut] = []
    upstream_changes: list[ChangeOut] = []
    conflicts: list[ConflictOut] = []


# --------------------------------------------------------------- eventi WS
class Event(BaseModel):
    type: str          # hello | proposal_submitted | proposal_updated | consent_given |
                       # proposal_rejected | proposal_merged | master_updated |
                       # developer_joined | developer_left
    message: str = ""
    proposal: Optional[ProposalSummary] = None
    revision: Optional[int] = None
    developer: Optional[str] = None
