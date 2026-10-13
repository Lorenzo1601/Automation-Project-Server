"""Branch di progetto lato client: testi in italiano e regole di abilitazione delle azioni (senza dipendenze Qt).

Le regole servono solo a mostrare/abilitare i pulsanti giusti: l'autorita' e' sempre il server (``can_write`` /
``can_delete`` di ``BranchOut`` e il ruolo sul progetto)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from common.roles import Role
from common.schemas_branches import MAX_PERSONAL_BRANCHES, BranchOut

MAIN_LABEL = "Principale"
KIND_LABELS = {"personal": "personale", "team": "team"}
STATE_LABELS = {"open": "aperto", "merged": "unito", "closed": "chiuso"}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def behind_text(b: BranchOut) -> str:
    """«da main rev N, M indietro» (o «aggiornato» se il principale non ha novita')."""
    return f"da main rev {b.base_main_revision}, " + (f"{b.behind} indietro" if b.behind else "aggiornato")


def sync_text(b: BranchOut) -> str:
    """Indietro/avanti rispetto al principale, per la tabella."""
    parts = [f"{b.behind} indietro" if b.behind else "", f"{b.ahead} avanti" if b.ahead else ""]
    return " · ".join(p for p in parts if p) or "allineato"


def summary_text(b: BranchOut) -> str:
    """«Personale · da main rev 3, 2 indietro» (con lo stato se non e' aperto)."""
    s = f"{kind_label(b.kind).capitalize()} · {behind_text(b)}"
    return s if b.state == "open" else f"{s} · {state_label(b.state)}"


def display_name(b: Optional[BranchOut], slug: Optional[str] = None) -> str:
    return b.name if b is not None else (slug or MAIN_LABEL)


@dataclass(frozen=True)
class BranchAbilities:
    """Cosa puo' fare l'utente su un branch (per abilitare i pulsanti)."""
    switch: bool        # «Passa a questo branch»
    commit: bool        # «Salva su branch...» (personale: revisione diretta; team: unione 3-way con consenso automatico)
    request_merge: bool  # «Richiedi Merge...» dentro il branch (solo branch team)
    sync: bool          # «Aggiorna dal principale»
    merge: bool         # «Unisci nel principale»
    close: bool
    delete: bool


def abilities(b: BranchOut, role: Role, current: Optional[str] = None) -> BranchAbilities:
    """Regole: Viewer sola lettura; merged/closed sola lettura; il branch personale lo scrive solo il proprietario
    (``can_write``); chiudere/eliminare dipende da ``can_delete``. «Aggiorna dal principale» ha senso solo se il
    principale ha novita' (``behind``)."""
    dev = role >= Role.DEVELOPER
    is_open = b.state == "open"
    write = dev and is_open and b.can_write
    return BranchAbilities(
        switch=b.slug != current,
        commit=write,
        request_merge=write and b.kind == "team",
        sync=write and b.behind > 0,
        merge=write,
        close=dev and is_open and b.can_delete,
        delete=dev and b.can_delete,
    )


def my_open_personal(branches: Iterable[BranchOut], username: str) -> int:
    return sum(1 for b in branches if b.kind == "personal" and b.state == "open" and b.owner == username)


def can_create(role: Role, kind: str, personal_open: int = 0) -> tuple[bool, str]:
    """(consentito, motivo se non lo e'). ``personal``: Developer o piu', max ``MAX_PERSONAL_BRANCHES`` aperti;
    ``team``: Lead o Admin."""
    if role < Role.DEVELOPER:
        return False, "Con il ruolo Viewer i branch si possono solo consultare."
    if kind == "team":
        return (True, "") if role >= Role.LEAD else (False, "I branch team li creano solo Lead e Administrator.")
    if personal_open >= MAX_PERSONAL_BRANCHES:
        return False, f"Hai già {MAX_PERSONAL_BRANCHES} branch personali aperti: chiudine o eliminane uno."
    return True, ""


def branch_choices(branches: Iterable[BranchOut], current: Optional[str]) -> list[BranchOut]:
    """Branch da mostrare nel selettore: quelli aperti piu' il corrente (anche se nel frattempo e' stato chiuso/unito)."""
    return [b for b in branches if b.state == "open" or b.slug == current]
