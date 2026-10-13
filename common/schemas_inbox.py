"""Schemi della posta in arrivo (notifiche persistenti)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: int
    ts: str
    kind: str
    title: str = ""
    message: str = ""
    project_id: Optional[int] = None
    project_name: str = ""
    branch: Optional[str] = None
    ref: Optional[str] = None            # id della richiesta di merge collegata
    read: bool = False


class UnreadOut(BaseModel):
    unread: int = 0


class MarkReadIn(BaseModel):
    ids: Optional[list[int]] = None      # None = tutte


class UpdateInfo(BaseModel):
    """Aggiornamento del client pubblicato sul server."""
    available: bool = False              # c'e' una versione piu' recente di quella del client che chiede
    version: str = ""                    # versione pubblicata (vuota se nessun client e' stato pubblicato)
    notes: str = ""
    sha256: str = ""
    size: int = 0
    filename: str = ""
    published_at: str = ""
    published_by: str = ""
