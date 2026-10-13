"""Tabella ``proposal_comments``: discussione a thread sulle richieste di merge (creata da ``create_all``)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.db.base import Base


class ProposalComment(Base):
    """Un commento su una richiesta di merge. Eliminare = soft delete (il testo diventa «[commento eliminato]»)."""
    __tablename__ = "proposal_comments"
    __table_args__ = (Index("ix_comments_proj_prop", "project_id", "proposal_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    proposal_id: Mapped[str] = mapped_column(String(32))
    author: Mapped[str] = mapped_column(String(64))
    author_role: Mapped[str] = mapped_column(String(32), default="")
    ts: Mapped[str] = mapped_column(String(40))                                  # ISO-8601 UTC
    text: Mapped[str] = mapped_column(Text, default="")
    reply_to: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    anchor: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    edited_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
