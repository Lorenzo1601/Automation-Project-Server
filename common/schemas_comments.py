"""Modelli pydantic della discussione sulle richieste di merge (commenti a thread)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

MAX_COMMENT_LEN = 2000
EDIT_WINDOW_S = 15 * 60                  # un commento si puo' modificare solo entro 15 minuti dalla pubblicazione
DELETED_TEXT = "[commento eliminato]"

MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][A-Za-z0-9_.\-]{0,63})")


def find_mentions(text: str) -> list[str]:
    """Nomi dopo «@» nel testo (minuscoli, senza duplicati, in ordine di apparizione); la punteggiatura finale e' esclusa."""
    seen: dict[str, None] = {}
    for m in MENTION_RE.finditer(text or ""):
        seen[m.group(1).rstrip(".-").lower()] = None
    return [n for n in seen if n]


class CommentIn(BaseModel):
    text: str
    reply_to: Optional[int] = None       # id del commento a cui si risponde (stessa richiesta)
    anchor: Optional[str] = None         # path del cambiamento o id del conflitto a cui il commento si riferisce


class CommentEdit(BaseModel):
    text: str


class CommentOut(BaseModel):
    id: int
    project_id: int
    proposal_id: str
    author: str
    author_role: str = ""
    ts: datetime
    text: str
    reply_to: Optional[int] = None
    anchor: Optional[str] = None
    edited_at: Optional[datetime] = None
    deleted: bool = False
    mentions: list[str] = []             # @username validi (esistono e hanno accesso al progetto)
