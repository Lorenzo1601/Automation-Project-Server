"""Tabella ``branches``: linee alternative del Master dentro un progetto (creata da ``create_all``).

I file di un branch (revisioni, storico, lock, proposte) stanno in ``data/projects/<slug>/branches/<slug del branch>/``;
qui restano solo i metadati. ``base_main_revision`` e' l'ultima revisione del principale gia' contenuta nel branch
(antenato comune per sync e merge); ``sync_revision`` la revisione del branch creata dall'ultimo sync (1 = creazione).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from server.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Branch(Base):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_branch_project_slug"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    slug: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))                              # team | personal
    owner: Mapped[str] = mapped_column(String(64), index=True)
    parent: Mapped[str] = mapped_column(String(64), default="", server_default="")   # slug del branch padre; "" = principale
    base_main_revision: Mapped[int] = mapped_column(Integer)
    sync_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    state: Mapped[str] = mapped_column(String(16), default="open", server_default="open")   # open | merged | closed
    merged_main_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
