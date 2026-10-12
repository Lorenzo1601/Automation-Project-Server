"""Reimport automatico dopo il merge delle proprie modifiche (funzioni pure, senza Qt).

Quando il merge di una mia richiesta si completa, il server confronta il Master con la mia revisione base: le
``upstream_changes`` con ``origin == "master"`` sono modifiche fatte da altri che io NON avevo; quelle con
``origin == "both"`` le avevo fatte anch'io (stesso risultato) e non contano.
"""
from __future__ import annotations

from typing import Iterable, Optional

from common.schemas import ChangeOut, ProposalDetail

MAX_LISTED = 5          # quante unita' toccate elencare nella notifica


def foreign_changes(detail: ProposalDetail) -> list[ChangeOut]:
    """Modifiche trovate sul Master che l'autore non aveva (solo ``origin == "master"``)."""
    return [c for c in detail.upstream_changes if c.origin == "master"]


def touched_units(changes: Iterable[ChangeOut], limit: int = MAX_LISTED) -> tuple[list[str], int]:
    """Unita' toccate (titolo, altrimenti percorso) senza doppioni: (prime ``limit``, quante altre ce ne sono)."""
    seen: list[str] = []
    for c in changes:
        label = (c.title or c.path or "").strip()
        if label and label not in seen:
            seen.append(label)
    return seen[:limit], max(0, len(seen) - limit)


def merge_notice(result_revision: Optional[int], changes: list[ChangeOut], file_name: str, file_revision: int,
                 kept_existing: bool = False) -> str:
    """Testo (italiano) della notifica all'utente dopo un reimport automatico."""
    n = len(changes)
    units, more = touched_units(changes)
    head = (f"Il tuo merge è stato completato come Master rev {result_revision}." if result_revision is not None
            else "Il tuo merge è stato completato.")
    found = (f"Il server ha trovato {n} {'modifica' if n == 1 else 'modifiche'} di altri sviluppatori "
             f"che non avevi")
    lines = [head, found + (":" if units else ".")]
    lines += [f"  • {u}" for u in units]
    if more:
        lines.append(f"  … e altre {more}")
    if kept_existing:
        lines.append(f"Il file «{file_name}» (rev {file_revision}) esisteva già con tue modifiche non unite: "
                     f"NON è stato sovrascritto, ma è ora la tua copia di lavoro (rev base {file_revision}).")
    else:
        lines.append(f"La tua copia di lavoro è stata aggiornata automaticamente a «{file_name}» "
                     f"(rev {file_revision}); il file precedente è rimasto invariato su disco.")
    return "\n".join(lines)


def manual_checkout_notice(result_revision: Optional[int], changes: list[ChangeOut], error: str) -> str:
    """Testo (italiano) quando il download automatico e' fallito: l'utente deve fare il Checkout a mano."""
    n = len(changes)
    head = (f"Il tuo merge è stato completato come Master rev {result_revision}." if result_revision is not None
            else "Il tuo merge è stato completato.")
    return (f"{head}\nIl server ha trovato {n} {'modifica' if n == 1 else 'modifiche'} di altri sviluppatori che non "
            f"avevi, ma l'aggiornamento automatico della copia di lavoro non è riuscito ({error}).\n"
            f"Esegui il Checkout manualmente per ripartire dal nuovo Master.")
