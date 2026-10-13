"""Schemi di confronto tra revisioni, etichette/blocco delle revisioni e lock esclusivo (presa in carico) del Master."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from common.schemas import ChangeOut, MasterInfo

LABEL_MAX = 80
LABEL_PRESETS = ("FAT", "SAT", "Consegna cliente", "Collaudo")      # suggerimenti del client (l'etichetta resta libera)
MIN_LOCK_REASON = 5                                                 # caratteri minimi del motivo di «Forza rilascio»
MIN_CHECKIN_MESSAGE = 5                                             # caratteri minimi del messaggio di check-in


# ---------------------------------------------------------------- confronto tra revisioni
class CompareOut(BaseModel):
    """Differenze tra due revisioni conservate, nello stesso formato ``ChangeOut`` dei merge."""
    from_info: MasterInfo
    to_info: MasterInfo
    kind: str
    changes: list[ChangeOut] = []
    n_added: int = 0
    n_modified: int = 0
    n_deleted: int = 0
    warnings: list[str] = []


# ---------------------------------------------------------------- etichette e blocco
class RevisionPatchIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=LABEL_MAX)    # "" = toglie l'etichetta
    locked: Optional[bool] = None                                       # True = blocca, False = sblocca (solo Admin)


# ---------------------------------------------------------------- lock (presa in carico)
class LockOut(BaseModel):
    locked: bool = False                      # False = Master libero
    holder: str = ""
    acquired_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    note: str = ""
    seconds_left: int = 0
    mine: bool = False                        # l'utente che chiede e' l'holder
    ttl_hours: float = 8.0                    # durata di una presa in carico / di un rinnovo


class LockIn(BaseModel):
    note: str = Field(default="", max_length=200)
