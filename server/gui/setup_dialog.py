"""Configurazione iniziale del server (primo avvio) e cambio della cartella dati."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from client.theme.tokens import T
from client.theme.widgets import icon, logo_pixmap
from client.views.common import heading, muted
from client.views.user_mgmt_view import suggest_password
from server.auth.security import validate_password_policy
from server.gui.controller import DATA_SUBDIR, check_writable, has_data, resolve_data_dir
from server.gui.settings import DEFAULT_PORT

HOSTS = (("Tutta la rete (consigliato)", "0.0.0.0"), ("Solo questo computer", "127.0.0.1"))


class FolderPicker(QWidget):
    """Campo cartella + 'Sfoglia' + anteprima di dove il server creera' davvero i suoi file."""

    def __init__(self, start: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.edit = QLineEdit(start or str(Path.home()))
        self.btn = QPushButton("  Sfoglia...")
        self.btn.setIcon(icon("folder", T["TEXT"], 18))
        self.btn.clicked.connect(self.browse)
        row.addWidget(self.edit, 1)
        row.addWidget(self.btn)
        lay.addLayout(row)
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        lay.addWidget(self.preview)
        self.edit.textChanged.connect(self._update)
        self._update()

    def browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Scegli la cartella in cui il server salvera' i dati", self.edit.text())
        if d:
            self.edit.setText(d)

    def base(self) -> str:
        return self.edit.text().strip()

    def target(self) -> Optional[Path]:
        return resolve_data_dir(self.base()) if self.base() else None

    def existing(self) -> bool:
        t = self.target()
        return bool(t and has_data(t))

    def _update(self) -> None:
        t = self.target()
        if t is None:
            self.preview.setText("")
            return
        if self.existing():
            msg = (f"<span style='color:{T['ORANGE']}'>Dati esistenti trovati</span> in <b>{t}</b>: "
                   "verranno riutilizzati (utenti, progetti e revisioni).")
        else:
            msg = f"Il server creera' la cartella <b>{t}</b> con database, progetti e revisioni."
        self.preview.setText(f"<span style='color:{T['MUTED']}'>{msg}</span>" if not self.existing() else msg)


class SetupDialog(QDialog):
    """Primo avvio: cartella dati, amministratore, rete."""

    def __init__(self, parent: Optional[QWidget] = None, base: str = "", host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        super().__init__(parent)
        self.setWindowTitle("Configurazione iniziale - Automation Project Server")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        lg = QLabel()
        lg.setPixmap(logo_pixmap(56))
        head.addWidget(lg)
        head.addSpacing(8)
        head.addWidget(heading("Configurazione del server", 2), 1)
        lay.addLayout(head)
        lay.addWidget(muted("Scegli dove il server salvera' il database, i progetti e tutte le revisioni. "
                            "Potrai cambiare cartella in seguito dalle Impostazioni."))

        lay.addWidget(self._label("CARTELLA DEI DATI"))
        self.folder = FolderPicker(base)
        lay.addWidget(self.folder)

        self.admin_box = QFrame()
        self.admin_box.setObjectName("Panel")
        al = QFormLayout(self.admin_box)
        al.setContentsMargins(16, 14, 16, 14)
        al.setSpacing(10)
        self.admin_user = QLineEdit("admin")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("min. 10 caratteri, lettere e cifre")
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        gen = QPushButton("Genera")
        gen.setProperty("variant", "ghost")
        gen.clicked.connect(self._generate)
        prow = QHBoxLayout()
        prow.addWidget(self.pw, 1)
        prow.addWidget(gen)
        al.addRow("Amministratore", self.admin_user)
        al.addRow("Password", prow)
        al.addRow("Ripeti password", self.pw2)
        self.admin_title = self._label("PRIMO AMMINISTRATORE")
        lay.addWidget(self.admin_title)
        lay.addWidget(self.admin_box)

        lay.addWidget(self._label("RETE"))
        net = QHBoxLayout()
        self.host = QComboBox()
        for text, val in HOSTS:
            self.host.addItem(text, val)
        self.host.setCurrentIndex(max(0, self.host.findData(host)))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(port)
        net.addWidget(self.host, 1)
        net.addWidget(QLabel("Porta"))
        net.addWidget(self.port)
        lay.addLayout(net)

        # HTTPS: attivo per default quando il server e' raggiungibile da tutta la rete (password e dati altrimenti in chiaro)
        self.tls = QCheckBox("Usa HTTPS con certificato autofirmato")
        self.tls.setToolTip("Cifra la connessione tra client e server. Il certificato viene creato in automatico nella "
                            "cartella dei dati; i client ne mostrano l'impronta alla prima connessione.")
        self.tls.setChecked(self.host.currentData() == "0.0.0.0")
        self._tls_touched = False
        self.tls.clicked.connect(lambda _c=False: setattr(self, "_tls_touched", True))
        self.host.currentIndexChanged.connect(self._sync_tls_default)
        lay.addWidget(self.tls)

        self.autostart = QCheckBox("Avvia il server automaticamente all'apertura dell'applicazione")
        self.autostart.setChecked(True)
        lay.addWidget(self.autostart)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{T['RED']};")
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Crea e avvia")
        bb.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        bb.button(QDialogButtonBox.Cancel).setText("Esci")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.folder.edit.textChanged.connect(self._sync_admin)
        self._sync_admin()

    @staticmethod
    def _label(text: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(f"color:{T['MUTED']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        return l

    def _sync_admin(self) -> None:
        ex = self.folder.existing()
        self.admin_box.setVisible(not ex)
        self.admin_title.setVisible(not ex)
        self.adjustSize()

    def _sync_tls_default(self) -> None:
        """Finche' l'utente non tocca la casella, HTTPS segue la scelta della rete (attivo con «Tutta la rete»)."""
        if not self._tls_touched:
            self.tls.setChecked(self.host.currentData() == "0.0.0.0")

    def _generate(self) -> None:
        pw = suggest_password()
        self.pw.setText(pw)
        self.pw2.setText(pw)
        self.pw.setEchoMode(QLineEdit.Normal)
        self.pw2.setEchoMode(QLineEdit.Normal)

    def _accept(self) -> None:
        if not self.folder.base():
            self.error.setText("Scegli la cartella dei dati.")
            return
        err = check_writable(self.folder.target().parent if not self.folder.target().exists() else self.folder.target())
        if err:
            self.error.setText(err)
            return
        if not self.folder.existing():
            if len(self.admin_user.text().strip()) < 3:
                self.error.setText("Amministratore: almeno 3 caratteri.")
                return
            try:
                validate_password_policy(self.pw.text())
            except ValueError as exc:
                self.error.setText(str(exc))
                return
            if self.pw.text() != self.pw2.text():
                self.error.setText("Le due password non coincidono.")
                return
        self.accept()

    def values(self) -> dict:
        return {"base": self.folder.base(), "admin_user": self.admin_user.text().strip().lower(),
                "admin_password": None if self.folder.existing() else self.pw.text(),
                "host": self.host.currentData(), "port": self.port.value(), "autostart": self.autostart.isChecked(),
                "tls": self.tls.isChecked()}


class ChangeFolderDialog(QDialog):
    """Cambio della cartella dati a server esistente: scelta della nuova posizione e copia dei dati attuali."""

    def __init__(self, current: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Cambia cartella dei dati")
        self.setMinimumWidth(600)
        self.current = Path(current)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        lay.addWidget(heading("Nuova cartella dei dati", 2))
        lay.addWidget(muted(f"Cartella attuale: {self.current}"))
        self.folder = FolderPicker(str(self.current.parent))
        lay.addWidget(self.folder)
        self.copy = QCheckBox("Copia nella nuova cartella i dati attuali (consigliato)")
        self.copy.setChecked(True)
        lay.addWidget(self.copy)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setProperty("muted", True)
        lay.addWidget(self.note)
        lay.addWidget(muted("Il server verra' fermato e riavviato. La cartella attuale non viene cancellata: "
                            "puoi eliminarla a mano dopo aver verificato che tutto funzioni."))
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{T['RED']};")
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Applica")
        bb.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.folder.edit.textChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        ex = self.folder.existing()
        self.copy.setEnabled(not ex)
        if ex:
            self.note.setText("Nella nuova cartella ci sono gia' dei dati: verranno usati quelli (nessuna copia).")
        elif self.copy.isChecked():
            self.note.setText("")
        else:
            self.note.setText("Senza copia il server parte da zero, con un nuovo amministratore.")
    
    def _accept(self) -> None:
        t = self.folder.target()
        if t is None:
            self.error.setText("Scegli la cartella.")
            return
        if t.resolve() == self.current.resolve():
            self.error.setText("E' gia' la cartella in uso.")
            return
        try:
            if t.resolve().is_relative_to(self.current.resolve()):
                self.error.setText("La nuova cartella non puo' trovarsi dentro quella attuale.")
                return
        except OSError:
            pass
        err = check_writable(t if t.exists() else t.parent)
        if err:
            self.error.setText(err)
            return
        self.accept()

    def values(self) -> tuple[str, bool]:
        return self.folder.base(), self.copy.isChecked() and self.copy.isEnabled()
