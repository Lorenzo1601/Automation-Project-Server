"""Tabella ``notifications``: posta in arrivo di ogni utente (resta anche se l'utente era offline)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.db.base import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_read", "user_id", "read"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    ts: Mapped[str] = mapped_column(String(40))                                   # ISO-8601 UTC
    kind: Mapped[str] = mapped_column(String(40))                                 # merge_request | merge_rejected | merge_merged | branch_updated | mention
    title: Mapped[str] = mapped_column(String(160), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    branch: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)         # id della richiesta (per aprirla)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
