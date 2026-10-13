"""Schermata di login: selezione server, autenticazione, verifica raggiungibilita' con LED di stato."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

from client import trust as _trust
from client.api_client import ApiClient
from client.discovery import ServerInfo, discover as discover_servers
from client.theme.tokens import T
from client.theme.widgets import HeroPanel, StatusLed, add_shadow, icon, icon_pixmap, logo_pixmap
from client.views.common import heading, muted, polish
from client.workers import run_async


class LoginView(QWidget):
    login_requested = Signal(str, str, str)          # url, username, password

    def __init__(self, servers: Optional[list[str]] = None, run: Callable = run_async,
                 parent: Optional[QWidget] = None, discover: Callable[[], list[ServerInfo]] = discover_servers):
        super().__init__(parent)
        self._run, self._discover = run, discover
        self._saved = ""
        self._found: list[ServerInfo] = []
        self.trust: Optional[_trust.TrustStore] = None            # archivio dei pin (default: configurazione utente)
        self._issue: tuple[str, str] = ("", "")                    # (url, tipo di problema di certificato rilevato)

        # ---------- pannello sinistro (marca): gradiente + motivo geometrico disegnato
        hero = HeroPanel()
        hero.setMinimumWidth(440)
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(64, 56, 64, 48)
        hl.setSpacing(0)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(84))
        logo.setFixedSize(84, 84)
        title = QLabel("AUTOMATION\nPROJECT SERVER")
        title.setStyleSheet(f"font-size:34px; font-weight:800; color:{T['TEXT']};")
        tag = QLabel("Collaborazione multi-utente offline sui progetti\nStudio 5000 (.L5K / .L5X) e su qualsiasi altro file")
        tag.setStyleSheet(f"font-size:14px; color:{T['TEXT_DIM']};")
        hl.addStretch(2)
        hl.addWidget(logo)
        hl.addSpacing(20)
        hl.addWidget(title)
        hl.addSpacing(12)
        hl.addWidget(tag)
        hl.addSpacing(40)
        for ic, head, text in (("merge", "3-way merge strutturale", "Rung, Tag, UDT e AOI fusi senza sovrascritture"),
                               ("shield", "Consenso e ruoli", "Approvazione del Lead, permessi per progetto"),
                               ("audit", "Audit trail immutabile", "Chi ha scaricato, proposto e approvato")):
            row = QHBoxLayout()
            row.setSpacing(14)
            tile = QFrame()
            tile.setObjectName("FeatureTile")
            tile.setFixedSize(40, 40)
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(0, 0, 0, 0)
            i = QLabel()
            i.setPixmap(icon_pixmap(ic, T["ACCENT"], 20))
            i.setAlignment(Qt.AlignCenter)
            tl.addWidget(i)
            col = QVBoxLayout()
            col.setSpacing(1)
            a = QLabel(head)
            a.setStyleSheet("font-weight:600; font-size:14px;")
            col.addWidget(a)
            col.addWidget(muted(text))
            row.addWidget(tile, 0, Qt.AlignTop)
            row.addLayout(col, 1)
            hl.addLayout(row)
            hl.addSpacing(18)
        hl.addStretch(3)
        foot = QLabel("Dati sempre sul tuo server · nessun cloud")
        foot.setStyleSheet(f"font-size:12px; color:{T['MUTED']};")
        hl.addWidget(foot)

        # ---------- scheda di accesso
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(440)
        card.setMinimumWidth(380)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(36, 34, 36, 32)
        cl.setSpacing(8)
        cl.addWidget(heading("Accedi", 1))
        cl.addWidget(muted("Usa le credenziali assegnate dall'amministratore del progetto."))
        cl.addSpacing(10)

        cl.addWidget(self._lab("SERVER"))
        self.server = QComboBox()
        self.server.setEditable(True)
        self.server.addItems(servers or ["http://localhost:8750"])
        self.server.lineEdit().setPlaceholderText("http://server:8750")
        self.server.lineEdit().editingFinished.connect(self.check_server)
        self.server.activated.connect(lambda _i: self.check_server())
        self.server.activated.connect(lambda _i: self._sync_found_selection())
        srow = QHBoxLayout()
        srow.setSpacing(6)
        srow.addWidget(self.server, 1)
        self.btn_search = QToolButton()
        self.btn_search.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_search.setToolTip("Cerca i server nella rete locale")
        self.btn_search.setMinimumHeight(36)
        self.btn_search.setCursor(Qt.PointingHandCursor)
        self.btn_search.clicked.connect(lambda: self.search_servers())
        srow.addWidget(self.btn_search)
        cl.addLayout(srow)
        st = QHBoxLayout()
        st.setSpacing(0)
        self.led = StatusLed(diameter=8)
        self.lbl_server = QLabel("Server non verificato")
        self.lbl_server.setProperty("muted", True)
        st.addWidget(self.led)
        st.addWidget(self.lbl_server, 1)
        cl.addLayout(st)
        # stato della cifratura della connessione (https = lucchetto verde; http = avviso discreto)
        cr = QHBoxLayout()
        cr.setSpacing(6)
        self.ico_crypt = QLabel()
        self.ico_crypt.setFixedSize(14, 14)
        self.lbl_crypt = QLabel("")
        self.lbl_crypt.setObjectName("CryptState")
        cr.addWidget(self.ico_crypt)
        cr.addWidget(self.lbl_crypt, 1)
        cl.addLayout(cr)
        self.server.editTextChanged.connect(lambda _t: self._update_crypt())
        self._update_crypt()

        # elenco dei server rilevati in rete (visibile quando la ricerca ne trova)
        self.found = QListWidget()
        self.found.setObjectName("FoundList")
        self.found.setVisible(False)
        self.found.setSelectionMode(QListWidget.SingleSelection)
        self.found.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.found.itemClicked.connect(self._found_clicked)
        cl.addWidget(self.found)

        cl.addSpacing(2)
        cl.addWidget(self._lab("UTENTE"))
        self.user = QLineEdit()
        self.user.setPlaceholderText("nome.cognome")
        cl.addWidget(self.user)
        cl.addSpacing(2)
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
        cl.addSpacing(2)
        cl.addWidget(self.remember)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty("error", True)
        self.error.setStyleSheet(f"color:{T['RED']}; background:transparent;")
        self.error.hide()
        cl.addWidget(self.error)
        cl.addSpacing(8)
        self.btn = QPushButton("Accedi")
        self.btn.setProperty("variant", "primary")
        self.btn.setMinimumHeight(42)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setDefault(True)
        self.btn.clicked.connect(self._submit)
        cl.addWidget(self.btn)
        self.user.returnPressed.connect(self.pw.setFocus)
        self.pw.returnPressed.connect(self._submit)
        add_shadow(card, blur=48, dy=14, alpha=170)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(32, 24, 32, 24)
        rl.addStretch(1)
        crow = QHBoxLayout()                 # (niente allineamento sul widget: con le etichette a capo la scheda verrebbe compressa)
        crow.addStretch(1)
        crow.addWidget(card)
        crow.addStretch(1)
        rl.addLayout(crow)
        rl.addStretch(1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(hero, 5)
        lay.addWidget(right, 4)

    @staticmethod
    def _lab(text: str) -> QLabel:
        l = QLabel(text)
        l.setProperty("eyebrow", True)
        f = l.font()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        l.setFont(f)
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
        self._fill_found(found)
        if not found:
            self.led.set_state("offline")
            self.lbl_server.setText("Nessun server trovato in rete: inserisci l'indirizzo a mano")
            return
        self.check_server()
        if len(found) == 1:
            self.lbl_server.setText(f"Trovato: {found[0].name}")
        else:
            self.lbl_server.setText(f"Trovati {len(found)} server: scegli dall'elenco")

    def _fill_found(self, found: list) -> None:
        """Elenco visibile dei server rilevati (un clic seleziona il server e lo verifica)."""
        self._found = list(found)
        self.found.blockSignals(True)
        self.found.clear()
        for info in found:
            it = QListWidgetItem(icon("server", T["ACCENT"], 18), f"{info.name}\n{info.host}:{info.port}")
            it.setData(Qt.UserRole, info.url)
            it.setSizeHint(QSize(0, 48))
            self.found.addItem(it)
        self.found.setFixedHeight(min(len(found), 3) * 48 + 12 if found else 0)
        self.found.setVisible(bool(found) and len(found) > 1)
        self.found.blockSignals(False)
        self._sync_found_selection()
        card_layout = self.found.parentWidget().layout() if self.found.parentWidget() else None
        if card_layout is not None:                                  # la scheda si ridisegna subito con l'elenco
            card_layout.invalidate()
            card_layout.activate()

    def _sync_found_selection(self) -> None:
        url = self.server_url()
        self.found.blockSignals(True)
        self.found.clearSelection()
        for r in range(self.found.count()):
            if self.found.item(r).data(Qt.UserRole) == url:
                self.found.setCurrentRow(r)
        self.found.blockSignals(False)

    def _found_clicked(self, it: QListWidgetItem) -> None:
        i = self.server.findData(it.data(Qt.UserRole))
        if i >= 0:
            self.server.setCurrentIndex(i)
            self._sync_found_selection()
            self.check_server()

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

        def probe() -> tuple[bool, str]:
            api = ApiClient(url, trust=self.trust)
            ok = api.health()
            return ok, (api.tls_issue.kind if api.tls_issue else "")

        def done(r: tuple[bool, str]) -> None:
            self._issue = (url, r[1])
            self._server_checked(*r)
        self._run(probe, done, lambda e: self._server_checked(False))

    def _server_checked(self, ok: bool, issue: str = "") -> None:
        if issue == "untrusted":                                    # il server risponde in TLS ma il certificato va confermato
            self.led.set_state("online")
            self.lbl_server.setText("Server raggiungibile: certificato da confermare all'accesso")
            self._update_crypt()
            return
        if issue == "changed":
            self.led.set_state("offline")
            self.lbl_server.setText("Certificato del server cambiato")
            self._update_crypt()
            return
        self.led.set_state("online" if ok else "offline")
        txt = self.lbl_server.text()
        if ok and (txt.startswith("Trovat") or txt.startswith("Ricerca")):
            self._update_crypt()
            return                                                   # lascia il messaggio della ricerca
        self.lbl_server.setText("Server raggiungibile" if ok else "Server non raggiungibile")
        self._update_crypt()

    def _update_crypt(self) -> None:
        """«Connessione cifrata» (lucchetto) per https://, «Connessione non cifrata» per http://."""
        url = self.server_url().lower()
        issue = self._issue[1] if self._issue[0].lower() == url else ""
        if url.startswith("https://"):
            if issue == "changed":
                text, color, lock = "Certificato CAMBIATO: possibile attacco, non inserire la password", T["RED"], T["RED"]
            elif issue == "untrusted":
                text, color, lock = "Connessione cifrata (certificato da confermare)", T["ORANGE"], T["ORANGE"]
            else:
                text, color, lock = "Connessione cifrata", T["GREEN"], T["GREEN"]
            self.ico_crypt.setPixmap(icon_pixmap("lock", lock, 14))
        elif url.startswith("http://"):
            text, color = "Connessione non cifrata", T["ORANGE"]
            self.ico_crypt.clear()
        else:
            text, color = "", T["MUTED"]
            self.ico_crypt.clear()
        self.lbl_crypt.setText(text)
        self.lbl_crypt.setStyleSheet(f"color:{color}; font-size:11px; background:transparent;")
        self.ico_crypt.setVisible(url.startswith("https://"))

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


class TrustDialog(QDialog):
    """Prima connessione a un server con certificato non verificabile: mostra l'impronta e chiede se fidarsi."""

    def __init__(self, host: str, port: int, summary: "_trust.CertSummary", reason: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Certificato del server")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.addWidget(heading("Considerare attendibile questo server?", 2))
        intro = muted(f"Ti stai collegando in modo sicuro a {host}:{port}, ma il suo certificato non e' emesso da un'autorita' "
                      "riconosciuta (tipico dei certificati autofirmati). Confronta l'impronta qui sotto con quella "
                      "comunicata dall'amministratore (Impostazioni del server > Rete > Connessione sicura).")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        form.addRow("Server", QLabel(f"{host}:{port}"))
        form.addRow("Intestato a", QLabel(summary.subject or "-"))
        form.addRow("Emesso da", QLabel("se stesso (autofirmato)" if summary.self_signed else (summary.issuer or "-")))
        form.addRow("Validita'", QLabel(summary.validity_text()))
        if summary.san:
            san = QLabel(", ".join(summary.san[:8]) + (" ..." if len(summary.san) > 8 else ""))
            san.setWordWrap(True)
            form.addRow("Nomi/indirizzi", san)
        lay.addLayout(form)
        lay.addWidget(self._lab("IMPRONTA SHA-256"))
        self.fp = QLabel(_trust.format_fingerprint(summary.sha256, per_line=16))
        self.fp.setProperty("mono", True)
        self.fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.fp)
        if summary.expired:
            warn = QLabel("Attenzione: il certificato risulta SCADUTO.")
            warn.setStyleSheet(f"color:{T['ORANGE']};")
            lay.addWidget(warn)
        lay.addWidget(muted("Se lo consideri attendibile, l'impronta viene ricordata: dalle volte successive la connessione "
                            "e' accettata solo se il certificato e' lo stesso. Se cambia, ti verra' segnalato."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Considera attendibile")
        bb.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        bb.button(QDialogButtonBox.Cancel).setText("Annulla")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @staticmethod
    def _lab(text: str) -> QLabel:
        l = QLabel(text)
        l.setProperty("eyebrow", True)
        return l


def ask_trust(parent: Optional[QWidget], host: str, port: int, summary: "_trust.CertSummary", reason: str = "") -> bool:
    """Hook predefinito di ``MainWindow.confirm_trust`` (sostituibile nei test)."""
    return TrustDialog(host, port, summary, reason, parent).exec() == QDialog.Accepted


def ask_forget_pin(parent: Optional[QWidget], host: str, port: int, message: str) -> bool:
    """Impronta cambiata: spiega il rischio e permette di rimuovere la fiducia salvata. True = rimuovere."""
    box = QMessageBox(QMessageBox.Warning, "Certificato del server cambiato", message, QMessageBox.NoButton, parent)
    forget = box.addButton("Rimuovi la fiducia salvata", QMessageBox.DestructiveRole)
    box.addButton("Chiudi", QMessageBox.RejectRole)
    box.setDefaultButton(box.buttons()[-1])
    box.exec()
    return box.clickedButton() is forget

