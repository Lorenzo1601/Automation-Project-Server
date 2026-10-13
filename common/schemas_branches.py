"""Branch di progetto (contratto condiviso da server e client).

Un branch e' una linea alternativa del Master dentro lo stesso progetto, con le sue revisioni.
* ``team``     creato da Lead/Admin: si lavora con le normali richieste di merge e il consenso, come sul principale.
* ``personal`` creato da un Developer (o piu' alto): solo il proprietario scrive, con salvataggi diretti.
Il branch principale e' quello normale del progetto (nessun parametro ``branch``).
Tutti gli endpoint ``/api/projects/{pid}/...`` (master, history, download, proposals, comments, lock, compare...)
accettano il parametro ``?branch=<slug>`` per operare sul branch; senza il parametro operano sul principale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

BranchKind = Literal["team", "personal"]
BranchState = Literal["open", "merged", "closed"]
MAX_PERSONAL_BRANCHES = 5            # branch personali aperti per utente e progetto


class BranchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)       # nome mostrato (lo slug si ricava: minuscolo, a-z0-9-_)
    kind: BranchKind = "personal"
    from_revision: Optional[int] = Field(default=None, ge=1)   # revisione del PRINCIPALE di partenza (None = testa)


class BranchOut(BaseModel):
    id: int
    name: str
    slug: str
    kind: BranchKind
    owner: str                                    # chi lo ha creato (proprietario dei branch personali)
    created_at: datetime
    state: BranchState = "open"
    base_main_revision: int                       # ultima revisione del principale gia' contenuta nel branch (antenato comune)
    head_revision: int                            # testa del branch
    behind: int = 0                               # revisioni nuove sul principale dopo base_main_revision
    ahead: int = 0                                # revisioni del branch dopo l'ultimo sync/merge (head - 1 - sync fatti)
    purged_revisions: int = 0
    merged_main_revision: Optional[int] = None    # revisione del principale prodotta dal merge del branch
    can_write: bool = False                       # l'utente corrente puo' scrivere sul branch
    can_delete: bool = False                      # l'utente corrente puo' chiudere/eliminare il branch
