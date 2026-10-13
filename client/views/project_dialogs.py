"""Dialog condivisi per la gestione dei progetti (usati dalla schermata principale e dalla scheda Utenti & Permessi)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from client.theme.tokens import T
from client.views.common import heading, muted
from common.schemas import ProjectCreate, ProjectOut, ProjectUpdate


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


class EditProjectDialog(QDialog):
    """Modifica di nome e descrizione di un progetto (solo Admin)."""

    def __init__(self, project: ProjectOut, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Modifica progetto")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.addWidget(heading("Modifica progetto", 2))
        form = QFormLayout()
        self.name = QLineEdit(project.name)
        self.desc = QLineEdit(project.description or "")
        form.addRow("Nome", self.name)
        form.addRow("Descrizione", self.desc)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        bb.accepted.connect(lambda: self.accept() if self.name.text().strip() else None)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def payload(self) -> ProjectUpdate:
        """Solo i campi cambiati (``None`` = invariato)."""
        name, desc = self.name.text().strip(), self.desc.text().strip()
        return ProjectUpdate(name=name if name != self.project.name else None,
                             description=desc if desc != (self.project.description or "") else None)


class DeleteProjectDialog(QDialog):
    """Conferma forte dell'eliminazione di un progetto: si abilita solo scrivendo il nome esatto."""

    def __init__(self, project: ProjectOut, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Elimina progetto")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(heading(f"Eliminare «{project.name}»?", 2))
        warn = QLabel("Operazione <b>definitiva e irreversibile</b>. Dal server spariranno per sempre:")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{T['RED']};")
        lay.addWidget(warn)
        self.lbl_effects = QLabel(
            "• tutti i file e tutte le revisioni del Master sul server;\n"
            "• le richieste di merge del progetto;\n"
            "• i permessi assegnati agli utenti su questo progetto;\n"
            "• TUTTO l'audit del progetto (le righe del registro relative a questo progetto).")
        self.lbl_effects.setWordWrap(True)
        lay.addWidget(self.lbl_effects)
        lay.addWidget(muted("I file che ciascun utente ha sul proprio PC non vengono toccati."))
        self.chk_export = QCheckBox("Esporta prima un archivio (.zip) del progetto")
        self.chk_export.setChecked(True)
        self.chk_export.setToolTip("Revisioni ancora conservate, storico e audit in CSV: ti verrà chiesto dove salvarlo")
        lay.addWidget(self.chk_export)
        lay.addWidget(QLabel(f"Per confermare scrivi il nome esatto del progetto:  <b>{project.name}</b>"))
        self.confirm = QLineEdit()
        self.confirm.setPlaceholderText(project.name)
        lay.addWidget(self.confirm)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("Annulla")
        self.btn_delete = QPushButton("Elimina definitivamente")
        self.btn_delete.setProperty("variant", "danger")
        self.btn_delete.setEnabled(False)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_delete)
        lay.addLayout(btns)
        self.confirm.textChanged.connect(self._typed)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_delete.clicked.connect(self.accept)
        self.btn_cancel.setDefault(True)

    def _typed(self, text: str) -> None:
        self.btn_delete.setEnabled(text == self.project.name)

    def typed_name(self) -> str:
        return self.confirm.text()

    def wants_export(self) -> bool:
        return self.chk_export.isChecked()
