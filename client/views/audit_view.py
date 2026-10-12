"""Cronologia eventi (Audit Trail): sola lettura, filtri, esportazione CSV, verifica integrita' (Admin)."""
from __future__ import annotations

import csv
import io
import json
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from client.theme.tokens import T
from client.theme.widgets import icon
from client.views.common import full_time, heading, item, make_table, muted
from common.roles import Role
from common.schemas import AuditOut, AuditVerifyOut

ACTIONS = {
    "login": "Accesso", "login_failed": "Accesso fallito", "login_blocked": "Accesso bloccato", "logout": "Uscita",
    "password_changed": "Password cambiata", "token_reuse_detected": "Riuso token rilevato",
    "session_revoked": "Sessione revocata", "user_created": "Utente creato", "user_updated": "Utente modificato",
    "project_created": "Progetto creato", "project_updated": "Progetto modificato",
    "master_initialized": "Master inizializzato", "master_downloaded": "Master scaricato",
    "preview_downloaded": "Anteprima scaricata", "merge_requested": "Merge richiesto",
    "conflicts_resolved": "Conflitti risolti", "consent_given": "Consenso dato", "merge_rejected": "Merge rifiutato",
    "merge_cancelled": "Merge annullato", "merge_completed": "Merge completato", "merge_forced": "Merge forzato",
    "master_restored": "Master ripristinato",
    "console_session": "Console server", "permission_granted": "Permesso assegnato", "permission_revoked": "Permesso revocato",
}
ACTION_COLORS = {
    "merge_completed": T["GREEN"], "consent_given": T["GREEN"], "merge_requested": T["ACCENT"],
    "merge_rejected": T["RED"], "login_failed": T["RED"], "login_blocked": T["RED"], "token_reuse_detected": T["RED"],
    "merge_forced": T["ORANGE"], "master_restored": T["ORANGE"], "session_revoked": T["ORANGE"],
}
HEADERS = ["Data", "Utente", "Azione", "Progetto", "Oggetto", "Dettagli", "Hash"]


def details_text(d: dict) -> str:
    """'chiave=valore' separati da virgola; liste unite con '/', valori vuoti omessi."""
    def fmt(v) -> str:                                                       # noqa: ANN001
        return "/".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
    return ", ".join(f"{k}={fmt(v)}" for k, v in d.items() if v not in ("", None, [], {}))


def to_csv(entries: list[AuditOut], project_names: Optional[dict[int, str]] = None) -> str:
    """Esporta le voci in CSV (UTF-8, separatore ';' per Excel in locale italiano)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "timestamp_utc", "utente", "azione", "progetto", "oggetto", "dettagli_json", "ip", "hash"])
    for e in entries:
        proj = (project_names or {}).get(e.project_id, e.project_id if e.project_id is not None else "")
        w.writerow([e.id, e.ts.isoformat(), e.actor, e.action, proj, e.target,
                    json.dumps(e.details, ensure_ascii=False, sort_keys=True), e.ip, e.hash])
    return buf.getvalue()


class AuditView(QWidget):
    query_requested = Signal(dict)            # {"all_projects": bool, "action": str|None, "actor": str|None}
    verify_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._entries: list[AuditOut] = []
        self._projects: dict[int, str] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(12)
        lay.addWidget(heading("Cronologia eventi"))
        lay.addWidget(muted("Registro immutabile: ogni voce è concatenata alla precedente con un hash SHA-256 e il "
                            "database rifiuta modifiche e cancellazioni."))

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.scope = QComboBox()
        self.scope.addItem("Progetto corrente", False)
        self.scope.addItem("Tutti i progetti visibili", True)
        self.action = QComboBox()
        self.action.addItem("Tutte le azioni", None)
        for k, v in ACTIONS.items():
            self.action.addItem(v, k)
        self.actor = QLineEdit()
        self.actor.setPlaceholderText("Filtra per utente")
        self.actor.setMaximumWidth(200)
        self.btn_refresh = QPushButton("  Aggiorna")
        self.btn_refresh.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_csv = QPushButton("  Esporta CSV")
        self.btn_csv.setIcon(icon("download", T["TEXT"], 18))
        self.btn_verify = QPushButton("  Verifica integrità")
        self.btn_verify.setIcon(icon("shield", T["ORANGE"], 18))
        self.btn_verify.setProperty("variant", "warning")
        self.btn_verify.setVisible(False)
        for w in (self.scope, self.action, self.actor, self.btn_refresh):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.btn_csv)
        bar.addWidget(self.btn_verify)
        lay.addLayout(bar)

        self.banner = QLabel("")
        self.banner.setTextFormat(Qt.RichText)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.table = make_table(HEADERS, stretch_col=5)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 170)
        self.table.setColumnWidth(3, 190)
        self.table.setColumnWidth(4, 150)
        self.table.setColumnWidth(6, 90)
        lay.addWidget(self.table, 1)
        self.count = QLabel("")
        self.count.setProperty("muted", True)
        lay.addWidget(self.count)

        self.btn_refresh.clicked.connect(self.request)
        self.action.activated.connect(lambda _i: self.request())
        self.scope.activated.connect(lambda _i: self.request())
        self.actor.returnPressed.connect(self.request)
        self.btn_csv.clicked.connect(lambda: self.export_csv())
        self.btn_verify.clicked.connect(self.verify_requested)

    # ------------------------------------------------------------------ API
    def lock_scope_to_all(self) -> None:
        """Per la console del server: nessun progetto 'corrente', si vedono sempre tutti gli eventi."""
        self.scope.setCurrentIndex(1)
        self.scope.hide()

    def set_role(self, role: Role) -> None:
        self.btn_verify.setVisible(role >= Role.ADMIN)

    def set_project_names(self, names: dict[int, str]) -> None:
        self._projects = names

    def filters(self) -> dict:
        return {"all_projects": bool(self.scope.currentData()), "action": self.action.currentData(),
                "actor": self.actor.text().strip() or None}

    def request(self) -> None:
        self.query_requested.emit(self.filters())

    def set_entries(self, entries: list[AuditOut]) -> None:
        self._entries = entries
        self.table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            col = ACTION_COLORS.get(e.action)
            proj = self._projects.get(e.project_id, "" if e.project_id is None else f"#{e.project_id}")
            dt = details_text(e.details)
            self.table.setItem(r, 0, item(full_time(e.ts)))
            self.table.setItem(r, 1, item(e.actor, bold=True))
            self.table.setItem(r, 2, item(ACTIONS.get(e.action, e.action), col, bold=bool(col)))
            self.table.setItem(r, 3, item(proj))
            self.table.setItem(r, 4, item(e.target))
            di = item(dt)
            di.setToolTip(dt)
            self.table.setItem(r, 5, di)
            hi = item(e.hash[:8], T["MUTED"])
            hi.setToolTip(f"#{e.id}\n{e.hash}")
            self.table.setItem(r, 6, hi)
        self.count.setText(f"{len(entries)} eventi")

    def set_verify_result(self, v: AuditVerifyOut) -> None:
        if v.ok:
            self.banner.setText(f"<span style='color:{T['GREEN']}; font-weight:700'>✔ Integrità verificata</span> "
                                f"<span style='color:{T['MUTED']}'>— {v.checked} voci, catena di hash coerente.</span>")
        else:
            self.banner.setText(f"<span style='color:{T['RED']}; font-weight:700'>✘ Catena di hash interrotta</span> "
                                f"<span style='color:{T['MUTED']}'>— prima voce non valida: #{v.first_bad_id} "
                                f"({v.checked} voci controllate).</span>")
        self.banner.show()

    def export_csv(self, path: Optional[str] = None) -> Optional[str]:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Esporta cronologia", "audit.csv", "CSV (*.csv)")
        if not path:
            return None
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(to_csv(self._entries, self._projects))
        return path
