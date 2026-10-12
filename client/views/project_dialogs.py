"""Dialog condivisi per la gestione dei progetti (usati dalla schermata principale e dalla scheda Utenti & Permessi)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QVBoxLayout, QWidget)

from client.views.common import heading
from common.schemas import ProjectCreate


class NewProjectDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Nuovo progetto")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.addWidget(heading("Nuovo progetto PLC", 2))
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Linea Imbottigliamento")
        self.desc = QLineEdit()
        self.lead = QCheckBox("Richiedi il Consenso Finale di un Lead per i merge dei Developer")
        self.lead.setChecked(True)
        form.addRow("Nome", self.name)
        form.addRow("Descrizione", self.desc)
        lay.addLayout(form)
        lay.addWidget(self.lead)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        bb.accepted.connect(lambda: self.accept() if self.name.text().strip() else None)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def payload(self) -> ProjectCreate:
        return ProjectCreate(name=self.name.text().strip(), description=self.desc.text().strip(),
                             require_lead_approval=self.lead.isChecked())
