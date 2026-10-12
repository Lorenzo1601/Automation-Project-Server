"""Ruoli RBAC condivisi tra server e client (modello ispirato a TIA Project Server)."""
from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    """Gerarchia: ogni ruolo include i permessi di quelli inferiori."""
    VIEWER = 1       # Viewer / Auditor        - sola lettura, storico e audit
    DEVELOPER = 2    # Developer / Contributor - checkout, merge request, consenso incrociato
    LEAD = 3         # Lead Engineer / Approver- consenso finale, gestione progetto
    ADMIN = 4        # Project Administrator   - utenti, permessi, forza merge, ripristino

    @property
    def key(self) -> str:
        return ROLE_KEYS[self]

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]

    @classmethod
    def parse(cls, value: "Role | str | int") -> "Role":
        if isinstance(value, Role):
            return value
        if isinstance(value, int):
            return cls(value)
        v = value.strip().lower().replace(" ", "_").replace("-", "_")
        for r, k in ROLE_KEYS.items():
            if v in (k.lower(), r.name.lower()):
                return r
        aliases = {"project_administrator": cls.ADMIN, "lead": cls.LEAD, "approver": cls.LEAD,
                   "lead_engineer_/_approver": cls.LEAD, "contributor": cls.DEVELOPER,
                   "auditor": cls.VIEWER}
        if v in aliases:
            return aliases[v]
        raise ValueError(f"Ruolo sconosciuto: {value!r}")


ROLE_KEYS = {Role.ADMIN: "Admin", Role.LEAD: "Lead_Engineer", Role.DEVELOPER: "Developer", Role.VIEWER: "Viewer"}
ROLE_LABELS = {
    Role.ADMIN: "Project Administrator",
    Role.LEAD: "Lead Engineer / Approver",
    Role.DEVELOPER: "Developer / Contributor",
    Role.VIEWER: "Viewer / Auditor",
}
ROLE_DESCRIPTIONS = {
    Role.ADMIN: "Controllo totale: utenti, permessi, progetti; puo' forzare merge, risolvere conflitti bloccati "
                "e ripristinare qualsiasi revisione del Master.",
    Role.LEAD: "Richiede merge e da' il Consenso Finale alle richieste dei Developer; gestisce il progetto.",
    Role.DEVELOPER: "Scarica i progetti, invia merge request e da' il proprio consenso ai merge incrociati.",
    Role.VIEWER: "Sola lettura: scarica gli L5X, consulta storico revisioni e cronologia eventi.",
}
