"""Schermata di login: selezione server, autenticazione, verifica raggiungibilita' con LED di stato."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QToolButton, QVBoxLayout, QWidget)

from client.api_client import ApiClient
from client.discovery import ServerInfo, discover as discover_servers
from client.theme.tokens import T
from client.theme.widgets import StatusLed, icon, logo_pixmap
from client.views.common import heading, muted, polish
from client.workers import run_async


class LoginView(QWidget):
    login_requested = Signal(str, str, str)          # url, username, password

    def __init__(self, servers: Optional[list[str]] = None, run: Callable = run_async,
                 parent: Optional[QWidget] = None, discover: Callable[[], list[ServerInfo]] = discover_servers):
        super().__init__(parent)
        self._run, self._discover = run, discover
        self._saved = ""

        # ---------- pannello sinistro (brand)
        hero = QFrame()
        hero.setObjectName("Sidebar")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(56, 56, 56, 56)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(84))
        title = QLabel("AUTOMATION\nPROJECT SERVER")
        title.setStyleSheet(f"font-size:30px; font-weight:800; letter-spacing:1px; color:{T['TEXT']};")
        tag = QLabel("Collaborazione multi-utente offline\nsui progetti Studio 5000 (.L5K / .L5X) e altri file")
        tag.setStyleSheet(f"font-size:14px; color:{T['MUTED']};")
        hl.addStretch(2)
        hl.addWidget(logo)
        hl.addSpacing(14)
        hl.addWidget(title)
        hl.addSpacing(10)
        hl.addWidget(tag)
        hl.addSpacing(36)
        for ic, head, text in (("merge", "3-way merge strutturale", "Rung, Tag, UDT e AOI fusi senza sovrascritture"),
                               ("shield", "Consenso e ruoli", "Approvazione del Lead, permessi per progetto"),
                               ("audit", "Audit trail immutabile", "Chi ha scaricato, proposto e approvato")):
            row = QHBoxLayout()
            i = QLabel()
            i.setPixmap(icon(ic, T["ACCENT"], 20).pixmap(20, 20))
            i.setFixedWidth(30)
            col = QVBoxLayout()
            a = QLabel(head)
            a.setStyleSheet("font-weight:600;")
            col.addWidget(a)
            col.addWidget(muted(text))
            row.addWidget(i, 0, Qt.AlignTop)
            row.addLayout(col)
            hl.addLayout(row)
            hl.addSpacing(10)
        hl.addStretch(3)

        # ---------- form
        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(420)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(32, 30, 32, 30)
        cl.setSpacing(8)
        cl.addWidget(heading("Accedi", 1))
        cl.addWidget(muted("Usa le credenziali assegnate dall'amministratore del progetto."))
        cl.addSpacing(8)

        cl.addWidget(self._lab("SERVER"))
        self.server = QComboBox()
        self.server.setEditable(True)
        self.server.addItems(servers or ["http://localhost:8750"])
        self.server.lineEdit().setPlaceholderText("http://server:8750")
        self.server.lineEdit().editingFinished.connect(self.check_server)
        self.server.activated.connect(lambda _i: self.check_server())
        srow = QHBoxLayout()
        srow.setSpacing(6)
        srow.addWidget(self.server, 1)
        self.btn_search = QToolButton()
        self.btn_search.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_search.setToolTip("Cerca i server nella rete locale")
        self.btn_search.setMinimumHeight(34)
        self.btn_search.clicked.connect(lambda: self.search_servers())
        srow.addWidget(self.btn_search)
        cl.addLayout(srow)
        st = QHBoxLayout()
        self.led = StatusLed(diameter=8)
        self.lbl_server = QLabel("Server non verificato")
        self.lbl_server.setProperty("muted", True)
        st.addWidget(self.led)
        st.addWidget(self.lbl_server, 1)
        cl.addLayout(st)

        cl.addWidget(self._lab("UTENTE"))
        self.user = QLineEdit()
        self.user.setPlaceholderText("nome.cognome")
        cl.addWidget(self.user)
        cl.addWidget(self._lab("PASSWORD"))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("••••••••••")
        self._eye = self.pw.addAction(icon("eye", T["MUTED"], 18), QLineEdit.TrailingPosition)
        self._eye.setCheckable(True)
        self._eye.toggled.connect(lambda on: self.pw.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        cl.addWidget(self.pw)
        self.remember = QCheckBox("Ricorda server e utente")
        self.remember.setChecked(True)
        cl.addWidget(self.remember)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{T['RED']}; background:transparent;")
        self.error.hide()
        cl.addWidget(self.error)
        cl.addSpacing(6)
        self.btn = QPushButton("Accedi")
        self.btn.setProperty("variant", "primary")
        self.btn.setMinimumHeight(40)
        self.btn.setDefault(True)
        self.btn.clicked.connect(self._submit)
        cl.addWidget(self.btn)
        self.user.returnPressed.connect(self.pw.setFocus)
        self.pw.returnPressed.connect(self._submit)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addStretch(1)
        rl.addWidget(card, 0, Qt.AlignHCenter)
        rl.addStretch(1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(hero, 5)
        lay.addWidget(right, 4)

    @staticmethod
    def _lab(text: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(f"color:{T['MUTED']}; font-size:11px; font-weight:700; letter-spacing:1px; background:transparent;")
        return l

    # ---------------------------------------------------------------- API
    def server_url(self) -> str:
        """URL scelto: se nel campo c'e' una voce dell'elenco (nome + indirizzo) vale l'URL associato, altrimenti il testo."""
        text = self.server.currentText().strip()
        i = self.server.findText(text)
        if i >= 0 and self.server.itemData(i):
            return str(self.server.itemData(i)).rstrip("/")
        return text.rstrip("/")

    # ---------------------------------------------------------------- ricerca automatica
    def search_servers(self, auto: bool = False) -> None:
        """Cerca i server in rete: 1 trovato -> selezionato da solo; piu' di uno -> elenco da cui scegliere."""
        self.btn_search.setEnabled(False)
        self.led.set_state("connecting")
        self.lbl_server.setText("Ricerca dei server in rete...")
        self._run(self._discover, self._servers_found, lambda _e: self._servers_found([]))

    def _servers_found(self, found: list) -> None:
        self.btn_search.setEnabled(True)
        typed = self.server_url()
        keep = self._saved or typed
        self.server.blockSignals(True)
        self.server.clear()
        for info in found:
            self.server.addItem(info.label, info.url)
        if keep and not any(i.url == keep for i in found):
            self.server.addItem(keep, None)                          # indirizzo salvato / inserito a mano
        if not self.server.count():
            self.server.addItem("http://localhost:8750", None)
        pick = next((i for i, f in enumerate(found) if f.url == keep), 0 if found else self.server.count() - 1)
        self.server.setCurrentIndex(pick)
        self.server.blockSignals(False)
        if not found:
            self.led.set_state("offline")
            self.lbl_server.setText("Nessun server trovato in rete: inserisci l'indirizzo a mano")
            return
        self.check_server()
        if len(found) == 1:
            self.lbl_server.setText(f"Trovato: {found[0].name}")
        else:
            self.lbl_server.setText(f"Trovati {len(found)} server: scegli dall'elenco")
            if self.isVisible() and keep not in {i.url for i in found}:
                QTimer.singleShot(0, self.server.showPopup)

    def prefill(self, server: str = "", username: str = "") -> None:
        self._saved = server
        if server:
            i = self.server.findData(server)
            if i < 0:
                i = self.server.findText(server)
            if i < 0:
                self.server.insertItem(0, server)
                i = 0
            self.server.setCurrentIndex(i)
        self.user.setText(username)
        (self.pw if username else self.user).setFocus()

    def set_busy(self, busy: bool) -> None:
        self.btn.setEnabled(not busy)
        self.btn.setText("Connessione..." if busy else "Accedi")
        for w in (self.server, self.user, self.pw):
            w.setEnabled(not busy)

    def set_error(self, msg: str) -> None:
        self.error.setText(msg)
        self.error.setVisible(bool(msg))
        for w in (self.user, self.pw):
            w.setProperty("invalid", bool(msg))
            polish(w)

    def check_server(self) -> None:
        url = self.server_url()
        if not url:
            return
        self.led.set_state("connecting")
        self.lbl_server.setText("Verifica server...")
        self._run(lambda: ApiClient(url).health(), self._server_checked, lambda e: self._server_checked(False))

    def _server_checked(self, ok: bool) -> None:
        self.led.set_state("online" if ok else "offline")
        txt = self.lbl_server.text()
        if ok and (txt.startswith("Trovat") or txt.startswith("Ricerca")):
            return                                                   # lascia il messaggio della ricerca
        self.lbl_server.setText("Server raggiungibile" if ok else "Server non raggiungibile")

    def _submit(self) -> None:
        if not self.user.text().strip() or not self.pw.text():
            self.set_error("Inserisci utente e password")
            return
        self.set_error("")
        self.login_requested.emit(self.server_url(), self.user.text().strip(), self.pw.text())


class PasswordChangeDialog(QDialog):
    """Cambio password (obbligatorio al primo accesso o dopo un reset da parte dell'Admin)."""

    def __init__(self, forced: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Cambio password")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.addWidget(heading("Cambia password", 2))
        lay.addWidget(muted("Devi scegliere una nuova password prima di continuare." if forced else
                            "Minimo 10 caratteri, con almeno una lettera e una cifra."))
        form = QFormLayout()
        self.old, self.new, self.new2 = QLineEdit(), QLineEdit(), QLineEdit()
        for e in (self.old, self.new, self.new2):
            e.setEchoMode(QLineEdit.Password)
        form.addRow("Password attuale", self.old)
        form.addRow("Nuova password", self.new)
        form.addRow("Ripeti nuova", self.new2)
        lay.addLayout(form)
        self.err = QLabel()
        self.err.setStyleSheet(f"color:{T['RED']};")
        lay.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | (QDialogButtonBox.NoButton if forced else QDialogButtonBox.Cancel))
        bb.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        bb.button(QDialogButtonBox.Ok).setText("Cambia password")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self) -> None:
        if self.new.text() != self.new2.text():
            self.err.setText("Le due password non coincidono")
        elif len(self.new.text()) < 10:
            self.err.setText("La password deve avere almeno 10 caratteri")
        else:
            self.accept()

    def values(self) -> tuple[str, str]:
        return self.old.text(), self.new.text()
