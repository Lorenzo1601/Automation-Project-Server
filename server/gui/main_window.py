"""Finestra di amministrazione del server: stato, utenti e permessi, cronologia, impostazioni."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from client.api_client import ApiError
from client.theme import mono_font
from client.theme.tokens import T
from client.theme.widgets import StatusLed, app_icon, icon
from client.views.audit_view import ACTIONS, ACTION_COLORS, AuditView
from client.views.common import ScrollStack, fit_to_screen, full_time, heading, item, make_table, muted
from client.views.sidebar import Sidebar
from client.views.user_mgmt_view import UserMgmtView
from client.workers import run_async
from common.roles import Role
from server import tls as tlsmod
from server.auth.security import generate_password
from server.backup import default_backup_name
from server.gui.controller import (DATA_SUBDIR, EmbeddedServer, check_writable, copy_data, has_data,
                                   resolve_data_dir)
from server.gui.settings import ServerPrefs
from server.gui.setup_dialog import HOSTS, ChangeFolderDialog, SetupDialog
from server.service_ctl import (ServiceConfig, is_windows, last_command_log, run_elevated, service_command,
                                service_status)

SERVER_NAV = (("status", "server", "Stato del server"), ("users", "users", "Utenti & Progetti"),
              ("audit", "audit", "Cronologia eventi"), ("settings", "sliders", "Impostazioni"))
NAV_INDEX = {"status": 0, "users": 1, "audit": 2, "settings": 3}


def open_folder(path: "str | Path") -> None:
    p = str(path)
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(p)):
        if sys.platform.startswith("win"):
            os.startfile(p)                                                       # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])                                         # noqa: S603,S607


def _label(text: str) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(f"color:{T['MUTED']}; font-size:10px; font-weight:700; letter-spacing:1px;")
    return l


def _stat(title: str) -> tuple[QFrame, QLabel]:
    f = QFrame()
    f.setObjectName("Panel")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 12, 16, 12)
    lay.setSpacing(2)
    v = QLabel("-")
    v.setStyleSheet("font-size:24px; font-weight:700;")
    lay.addWidget(_label(title))
    lay.addWidget(v)
    return f, v


# ================================================================================================ pagina Stato
class StatusPage(QWidget):
    start_requested = Signal()
    stop_requested = Signal()
    restart_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(14)
        lay.addWidget(heading("Stato del server"))

        top = QFrame()
        top.setObjectName("Panel")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(18, 14, 18, 14)
        self.led = StatusLed(diameter=12)
        tl.addWidget(self.led)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.lbl_state = QLabel("Server fermo")
        self.lbl_state.setStyleSheet("font-size:17px; font-weight:700;")
        self.lbl_detail = QLabel("")
        self.lbl_detail.setProperty("muted", True)
        col.addWidget(self.lbl_state)
        col.addWidget(self.lbl_detail)
        tl.addLayout(col, 1)
        self.btn_start = QPushButton("  Avvia")
        self.btn_start.setIcon(icon("power", "#04180D", 18))
        self.btn_start.setProperty("variant", "success")
        self.btn_stop = QPushButton("  Arresta")
        self.btn_stop.setIcon(icon("power", T["RED"], 18))
        self.btn_stop.setProperty("variant", "danger")
        self.btn_restart = QPushButton("  Riavvia")
        self.btn_restart.setIcon(icon("refresh", T["TEXT"], 18))
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            tl.addWidget(b)
        lay.addWidget(top)
        self.btn_start.clicked.connect(self.start_requested)
        self.btn_stop.clicked.connect(self.stop_requested)
        self.btn_restart.clicked.connect(self.restart_requested)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.st_users = _stat("UTENTI ATTIVI")
        self.st_projects = _stat("PROGETTI")
        self.st_clients = _stat("CLIENT COLLEGATI")
        for f, _ in (self.st_users, self.st_projects, self.st_clients):
            stats.addWidget(f)
        lay.addLayout(stats)

        info = QFrame()
        info.setObjectName("Panel")
        il = QVBoxLayout(info)
        il.setContentsMargins(18, 14, 18, 14)
        il.setSpacing(6)
        il.addWidget(_label("INDIRIZZO DA USARE NEI CLIENT"))
        row = QHBoxLayout()
        self.lbl_urls = QLabel("-")
        self.lbl_urls.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_urls.setProperty("mono", True)
        self.btn_copy = QPushButton("  Copia")
        self.btn_copy.setIcon(icon("copy", T["TEXT"], 16))
        self.btn_copy.clicked.connect(self._copy_url)
        row.addWidget(self.lbl_urls, 1)
        row.addWidget(self.btn_copy)
        il.addLayout(row)
        il.addWidget(_label("CARTELLA DEI DATI"))
        row2 = QHBoxLayout()
        self.lbl_data = QLabel("-")
        self.lbl_data.setProperty("mono", True)
        self.lbl_data.setWordWrap(True)
        self.btn_open = QPushButton("  Apri")
        self.btn_open.setIcon(icon("folder", T["TEXT"], 16))
        row2.addWidget(self.lbl_data, 1)
        row2.addWidget(self.btn_open)
        il.addLayout(row2)
        lay.addWidget(info)

        lay.addWidget(_label("ATTIVITA' RECENTE"))
        self.recent = make_table(["Data", "Utente", "Azione", "Oggetto"], stretch_col=3)
        self.recent.setMaximumHeight(280)
        self.recent.setColumnWidth(0, 150)
        self.recent.setColumnWidth(1, 130)
        self.recent.setColumnWidth(2, 180)
        lay.addWidget(self.recent)

        lay.addWidget(_label("REGISTRO DEL SERVER"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFont(mono_font(9.5))
        self.log.setMinimumHeight(90)
        lay.addWidget(self.log, 1)
        self._first_url = ""
        self.set_state("offline")

    # ------------------------------------------------------------------ API
    def set_state(self, state: str, urls: Optional[list[str]] = None, data_dir: str = "", since: Optional[float] = None,
                  error: str = "", service: bool = False) -> None:
        self.led.set_state(state)
        self.lbl_state.setText({"online": "Server in esecuzione", "connecting": "Avvio in corso...",
                                "offline": "Server fermo"}[state])
        if state == "online" and since:
            self.lbl_detail.setText("Avviato alle " + time.strftime("%H:%M:%S", time.localtime(since)))
        elif error:
            self.lbl_detail.setText(f"<span style='color:{T['RED']}'>{error}</span>")
        else:
            self.lbl_detail.setText("I client non possono collegarsi finche' il server e' fermo; "
                                    "la gestione di utenti e progetti resta disponibile.")
        if service:
            self.lbl_state.setText("Server in esecuzione come servizio Windows")
            self.lbl_detail.setText("Parte con il PC e viene riavviato da Windows. Arresto e disinstallazione dalle Impostazioni.")
        self.btn_start.setEnabled(state == "offline" and not service)
        self.btn_stop.setEnabled(state == "online" and not service)
        self.btn_restart.setEnabled(state == "online" and not service)
        if urls is not None:
            self._first_url = next((u for u in urls if "localhost" not in u), urls[0] if urls else "")
            self.lbl_urls.setText("\n".join(urls) if urls else "-")
        if data_dir:
            self.lbl_data.setText(data_dir)

    def set_stats(self, users: int, projects: int, clients: int) -> None:
        self.st_users[1].setText(str(users))
        self.st_projects[1].setText(str(projects))
        self.st_clients[1].setText(str(clients))

    def set_clients(self, n: int) -> None:
        self.st_clients[1].setText(str(n))

    def set_recent(self, entries: list) -> None:
        self.recent.setRowCount(len(entries))
        for r, e in enumerate(entries):
            self.recent.setItem(r, 0, item(full_time(e.ts)))
            self.recent.setItem(r, 1, item(e.actor))
            self.recent.setItem(r, 2, item(ACTIONS.get(e.action, e.action), ACTION_COLORS.get(e.action)))
            self.recent.setItem(r, 3, item(e.target or ""))

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _copy_url(self) -> None:
        if self._first_url:
            QGuiApplication.clipboard().setText(self._first_url)


# ================================================================================================ pagina Impostazioni
class SettingsPage(QWidget):
    change_folder_requested = Signal()
    network_apply_requested = Signal(str, int, str)
    autostart_changed = Signal(bool)
    service_action = Signal(str)                      # install | start | stop | remove
    backup_requested = Signal()
    tls_generate_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(14)
        lay.addWidget(heading("Impostazioni"))

        box = QFrame()
        box.setObjectName("Panel")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(18, 14, 18, 14)
        bl.setSpacing(8)
        bl.addWidget(_label("CARTELLA DEI DATI DEL SERVER"))
        bl.addWidget(muted("Qui il server salva database, utenti, progetti, revisioni e audit trail."))
        self.lbl_data = QLabel("-")
        self.lbl_data.setProperty("mono", True)
        self.lbl_data.setWordWrap(True)
        bl.addWidget(self.lbl_data)
        row = QHBoxLayout()
        self.btn_change = QPushButton("  Cambia cartella...")
        self.btn_change.setIcon(icon("folder", T["TEXT"], 18))
        self.btn_open = QPushButton("  Apri la cartella")
        self.btn_open.setProperty("variant", "ghost")
        row.addWidget(self.btn_change)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)
        self.btn_change.clicked.connect(self.change_folder_requested)

        net = QFrame()
        net.setObjectName("Panel")
        nl = QVBoxLayout(net)
        nl.setContentsMargins(18, 14, 18, 14)
        nl.setSpacing(8)
        nl.addWidget(_label("RETE"))
        rn = QHBoxLayout()
        rn.addWidget(QLabel("Nome del server"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("nome mostrato nell'elenco dei server dei client (default: nome del PC)")
        rn.addWidget(self.name, 1)
        nl.addLayout(rn)
        r2 = QHBoxLayout()
        self.host = QComboBox()
        for text, val in HOSTS:
            self.host.addItem(text, val)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.btn_apply = QPushButton("Applica e riavvia")
        r2.addWidget(self.host, 1)
        r2.addWidget(QLabel("Porta"))
        r2.addWidget(self.port)
        r2.addWidget(self.btn_apply)
        nl.addLayout(r2)
        nl.addWidget(muted("Con \"Tutta la rete\" i client trovano il server da soli (UDP 8751) e si collegano alla porta "
                           "indicata: aprile nel firewall di Windows. La ricerca automatica funziona nella stessa rete "
                           "locale (non attraverso router/VPN). Senza HTTPS (sezione qui sotto) password e dati "
                           "viaggiano in chiaro: usare solo su reti fidate."))

        # ---- connessione sicura (HTTPS): certificato autofirmato generato qui oppure PEM dell'utente
        nl.addSpacing(6)
        nl.addWidget(_label("CONNESSIONE SICURA (HTTPS)"))
        self.tls_on = QCheckBox("Usa HTTPS")
        nl.addWidget(self.tls_on)
        self.tls_mode_self = QRadioButton("Genera certificato autofirmato")
        self.tls_mode_custom = QRadioButton("Usa un mio certificato (.pem/.crt + .key)...")
        self.tls_mode_self.setChecked(True)
        nl.addWidget(self.tls_mode_self)
        gr = QHBoxLayout()
        self.btn_tls_generate = QPushButton("Genera / rigenera certificato")
        self.btn_tls_generate.setProperty("variant", "ghost")
        self.btn_tls_generate.clicked.connect(self.tls_generate_requested)
        gr.addSpacing(22)
        gr.addWidget(self.btn_tls_generate)
        gr.addStretch(1)
        nl.addLayout(gr)
        nl.addWidget(self.tls_mode_custom)
        self.tls_cert, self.tls_key = QLineEdit(), QLineEdit()
        self.tls_cert.setPlaceholderText("certificato (.pem / .crt)")
        self.tls_key.setPlaceholderText("chiave privata (.key / .pem, senza password)")
        self.btn_tls_cert, self.btn_tls_key = QPushButton("Sfoglia..."), QPushButton("Sfoglia...")
        for edit, btn, title in ((self.tls_cert, self.btn_tls_cert, "Certificato"), (self.tls_key, self.btn_tls_key, "Chiave privata")):
            row = QHBoxLayout()
            row.addSpacing(22)
            row.addWidget(edit, 1)
            row.addWidget(btn)
            nl.addLayout(row)
            btn.clicked.connect(lambda _c=False, e=edit, t=title: self._browse_tls(e, t))
        self.lbl_cert = QLabel("-")
        self.lbl_cert.setProperty("mono", True)
        self.lbl_cert.setWordWrap(True)
        self.lbl_cert.setTextInteractionFlags(Qt.TextSelectableByMouse)
        nl.addWidget(self.lbl_cert)
        self.lbl_tls_warn = QLabel("")
        self.lbl_tls_warn.setWordWrap(True)
        self.lbl_tls_warn.setStyleSheet(f"color:{T['ORANGE']};")
        nl.addWidget(self.lbl_tls_warn)
        nl.addWidget(muted("Con HTTPS il server ascolta solo in https:// / wss:// (nessun HTTP in chiaro). Con il certificato "
                           "autofirmato i client mostrano l'impronta alla prima connessione e la ricordano; se rigeneri il "
                           "certificato dovranno accettarlo di nuovo. Per un certificato aziendale scegli i file PEM."))
        trow = QHBoxLayout()
        self.btn_tls_apply = QPushButton("Applica e riavvia")
        trow.addStretch(1)
        trow.addWidget(self.btn_tls_apply)
        nl.addLayout(trow)
        self.btn_tls_apply.clicked.connect(lambda: self.btn_apply.click())
        for w in (self.tls_on, self.tls_mode_self, self.tls_mode_custom):
            w.toggled.connect(self._sync_tls)
        self.host.currentIndexChanged.connect(self._sync_tls)
        self._tls_info = ""
        self._sync_tls()

        self.autostart = QCheckBox("Avvia il server automaticamente all'apertura dell'applicazione")
        nl.addWidget(self.autostart)
        lay.addWidget(net)

        bk = QFrame()
        bk.setObjectName("Panel")
        kl = QVBoxLayout(bk)
        kl.setContentsMargins(18, 14, 18, 14)
        kl.setSpacing(8)
        kl.addWidget(_label("BACKUP"))
        kl.addWidget(muted("Crea un archivio .zip con database, utenti, progetti, revisioni e audit; si puo' fare anche a "
                           "server acceso. L'archivio contiene i dati dei progetti e il segreto di firma dei token: "
                           "conservalo in un posto protetto. Per ripristinarlo: ferma il server e scompatta l'archivio "
                           "nella cartella dei dati."))
        brow = QHBoxLayout()
        self.btn_backup = QPushButton("  Crea backup...")
        self.btn_backup.setIcon(icon("folder", T["TEXT"], 18))
        self.btn_backup.clicked.connect(self.backup_requested)
        brow.addWidget(self.btn_backup)
        brow.addStretch(1)
        kl.addLayout(brow)
        lay.addWidget(bk)

        svc = QFrame()
        svc.setObjectName("Panel")
        sl = QVBoxLayout(svc)
        sl.setContentsMargins(18, 14, 18, 14)
        sl.setSpacing(8)
        sl.addWidget(_label("SERVIZIO WINDOWS"))
        sl.addWidget(muted("Installato come servizio, il server parte con il PC anche senza nessun utente collegato e "
                           "Windows lo riavvia se si ferma: non serve aprire questa applicazione. L'applicazione resta "
                           "utilizzabile per gestire utenti e progetti mentre il servizio e' attivo."))
        self.lbl_service = QLabel("-")
        self.lbl_service.setStyleSheet("font-weight:600;")
        sl.addWidget(self.lbl_service)
        srow = QHBoxLayout()
        self.btn_svc_install = QPushButton("Installa come servizio...")
        self.btn_svc_start = QPushButton("Avvia")
        self.btn_svc_stop = QPushButton("Arresta")
        self.btn_svc_remove = QPushButton("Disinstalla")
        self.btn_svc_remove.setProperty("variant", "danger")
        for b, a in ((self.btn_svc_install, "install"), (self.btn_svc_start, "start"), (self.btn_svc_stop, "stop"),
                     (self.btn_svc_remove, "remove")):
            b.clicked.connect(lambda _c=False, act=a: self.service_action.emit(act))
            srow.addWidget(b)
        srow.addStretch(1)
        sl.addLayout(srow)
        lay.addWidget(svc)
        self.set_service_status("unavailable" if not is_windows() else "stopped")
        lay.addStretch(1)
        self.btn_apply.clicked.connect(lambda: self.network_apply_requested.emit(self.host.currentData(), self.port.value(), self.name.text().strip()))
        self.autostart.toggled.connect(self.autostart_changed)

    def _browse_tls(self, edit: QLineEdit, title: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"{title} (PEM)", edit.text(),
                                              "Certificati e chiavi PEM (*.pem *.crt *.cer *.key);;Tutti i file (*)")
        if path:
            edit.setText(path)

    def _sync_tls(self) -> None:
        on = self.tls_on.isChecked()
        custom = self.tls_mode_custom.isChecked()
        for w in (self.tls_mode_self, self.tls_mode_custom):
            w.setEnabled(on)
        self.btn_tls_generate.setEnabled(on and not custom)
        for w in (self.tls_cert, self.tls_key, self.btn_tls_cert, self.btn_tls_key):
            w.setEnabled(on and custom)
        warn = tlsmod.insecure_warning(self.host.currentData() or "", on)
        self.lbl_tls_warn.setText(warn or "")
        self.lbl_tls_warn.setVisible(bool(warn))

    def tls_values(self) -> dict:
        """Scelte della sezione HTTPS: ``enabled``, ``mode`` (self | custom), ``cert`` e ``key`` (percorsi PEM dell'utente)."""
        return {"enabled": self.tls_on.isChecked(), "mode": "custom" if self.tls_mode_custom.isChecked() else "self",
                "cert": self.tls_cert.text().strip(), "key": self.tls_key.text().strip()}

    def load_tls(self, enabled: bool, mode: str, cert: str, key: str, info: str = "") -> None:
        for w in (self.tls_on, self.tls_mode_self, self.tls_mode_custom):
            w.blockSignals(True)
        self.tls_on.setChecked(enabled)
        (self.tls_mode_custom if mode == "custom" else self.tls_mode_self).setChecked(True)
        for w in (self.tls_on, self.tls_mode_self, self.tls_mode_custom):
            w.blockSignals(False)
        self.tls_cert.setText(cert)
        self.tls_key.setText(key)
        self._tls_info = info
        self.lbl_cert.setText(info or "Nessun certificato ancora: verra' creato all'attivazione di HTTPS.")
        self._sync_tls()

    def set_service_status(self, st: str) -> None:
        text = {"running": ("Installato - in esecuzione", T["GREEN"]), "stopped": ("Installato - fermo", T["ORANGE"]),
                "pending": ("Installato - in transizione...", T["ORANGE"]), "not_installed": ("Non installato", T["MUTED"]),
                "unavailable": ("Disponibile solo su Windows", T["MUTED"])}[st]
        self.lbl_service.setText(text[0])
        self.lbl_service.setStyleSheet(f"font-weight:600; color:{text[1]};")
        self.service_state = st
        ok = st != "unavailable"
        self.btn_svc_install.setEnabled(ok and st == "not_installed")
        self.btn_svc_start.setEnabled(ok and st == "stopped")
        self.btn_svc_stop.setEnabled(ok and st == "running")
        self.btn_svc_remove.setEnabled(ok and st in ("running", "stopped", "pending"))

    def load(self, data_dir: str, host: str, port: int, autostart: bool, name: str = "") -> None:
        self.lbl_data.setText(data_dir)
        self.name.setText(name)
        self.host.setCurrentIndex(max(0, self.host.findData(host)))
        self.port.setValue(port)
        self.autostart.blockSignals(True)
        self.autostart.setChecked(autostart)
        self.autostart.blockSignals(False)


# ================================================================================================ finestra
class ServerWindow(QMainWindow):
    log_line = Signal(str)

    def __init__(self, prefs: Optional[ServerPrefs] = None, run: Callable = run_async,
                 server_factory: Callable[..., EmbeddedServer] = EmbeddedServer, confirm_close: bool = True,
                 stats_ms: int = 5000) -> None:
        super().__init__()
        self.prefs = prefs or ServerPrefs()
        self._run, self._factory, self._confirm_close = run, server_factory, confirm_close
        self.server: Optional[EmbeddedServer] = None
        self.service_active = False                                 # il server gira come servizio Windows sugli stessi dati
        self._pending_admin: Optional[tuple[str, str]] = None        # (utente, password) per il primo avvio
        self.setWindowTitle("Automation Project Server")
        self.setWindowIcon(app_icon())
        fit_to_screen(self, 1240, 800)
        self.workspace: Optional[QWidget] = None
        self._timer = QTimer(self, interval=stats_ms)
        self._timer.timeout.connect(self.refresh_stats)

    # ================================================================ avvio
    def start(self) -> bool:
        """Mostra la configurazione iniziale se serve, apre i dati e (se previsto) avvia il server."""
        if self.prefs.needs_setup:
            dlg = SetupDialog(self, host=self.prefs.host, port=self.prefs.port)
            if dlg.exec() != SetupDialog.Accepted:
                return False
            v = dlg.values()
            self.prefs.data_base, self.prefs.host, self.prefs.port = v["base"], v["host"], v["port"]
            self.prefs.autostart = v["autostart"]
            self.prefs.tls_enabled, self.prefs.tls_mode = bool(v.get("tls")), "self"
            if v["admin_password"]:
                self._pending_admin = (v["admin_user"], v["admin_password"])
        if not self._open_server():
            return False
        if self.prefs.autostart:
            self.start_server()
        return True

    def _make_server(self, admin: Optional[tuple[str, str]] = None) -> EmbeddedServer:
        srv = self._factory(resolve_data_dir(self.prefs.data_base), self.prefs.host, self.prefs.port,
                            admin_user=admin[0] if admin else None, admin_password=admin[1] if admin else None,
                            name=self.prefs.server_name, **self._tls_kwargs())
        srv.log.on_line = self.log_line.emit
        return srv

    def _tls_kwargs(self) -> dict:
        custom = self.prefs.tls_mode == "custom"
        return {"tls_enabled": self.prefs.tls_enabled, "tls_certfile": self.prefs.tls_certfile if custom else "",
                "tls_keyfile": self.prefs.tls_keyfile if custom else ""}

    def _open_server(self, admin: Optional[tuple[str, str]] = None) -> bool:
        admin = admin or self._pending_admin
        self._pending_admin = None
        try:
            self.server = self._make_server(admin)
            self.server.open()
        except Exception as exc:                                                   # noqa: BLE001
            QMessageBox.critical(self, "Impossibile aprire i dati",
                                 f"Cartella: {resolve_data_dir(self.prefs.data_base)}\n\n{exc}\n\n"
                                 "Controlla i permessi della cartella o scegline un'altra dalla configurazione.")
            self.prefs.data_base = ""
            self.server = None
            return False
        self._build_workspace()
        if self.server.bootstrap_password:
            QMessageBox.information(
                self, "Amministratore creato",
                f"Utente: {self.server.app.state.settings.admin_username}\nPassword: {self.server.bootstrap_password}\n\n"
                "Annotala: non verra' mostrata di nuovo. Potrai cambiarla dalla pagina Utenti & Progetti.")
        return True

    # ================================================================ UI
    def _build_workspace(self) -> None:
        srv = self.server
        assert srv is not None and srv.console is not None
        api = srv.console
        if self.workspace is not None:
            self.workspace.hide()
            self.workspace.deleteLater()
        self.sidebar = Sidebar(nav=SERVER_NAV, subtitle="Server", show_projects=False, admin_only=(),
                               show_password=False)
        self.sidebar.btn_out.hide()
        self.status = StatusPage()
        self.users = UserMgmtView(api, run=self._run)
        self.audit = AuditView()
        self.audit.set_role(Role.ADMIN)
        self.audit.lock_scope_to_all()
        self.settings_page = SettingsPage()
        self.pages = ScrollStack()
        for w in (self.status, self.users, self.audit, self.settings_page):
            self.pages.addWidget(w)
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.sidebar)
        lay.addWidget(self.pages, 1)
        self.workspace = root
        self.setCentralWidget(root)

        self.sidebar.set_user(api.user)
        self.sidebar.lbl_user.setText("Console locale")
        self.sidebar.lbl_user.setToolTip("Accesso diretto al server: non richiede credenziali")
        self.sidebar.nav_changed.connect(self._nav)
        self.status.start_requested.connect(self.start_server)
        self.status.stop_requested.connect(self.stop_server)
        self.status.restart_requested.connect(self.restart_server)
        self.status.btn_open.clicked.connect(lambda: open_folder(self.server.data_dir))
        self.settings_page.btn_open.clicked.connect(lambda: open_folder(self.server.data_dir))
        self.settings_page.change_folder_requested.connect(self.change_data_folder)
        self.settings_page.network_apply_requested.connect(
            lambda h, p, n: self.apply_network(h, p, n, tls=self.settings_page.tls_values()))
        self.settings_page.tls_generate_requested.connect(self.generate_certificate)
        self.settings_page.service_action.connect(self.service_action)
        self.settings_page.backup_requested.connect(self.create_backup)
        self.settings_page.autostart_changed.connect(lambda v: setattr(self.prefs, "autostart", v))
        self.audit.query_requested.connect(self._audit_query)
        self.audit.verify_requested.connect(self._audit_verify)
        self.users.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.users.projects_changed.connect(self.refresh_stats)
        self.log_line.connect(self.status.append_log)
        for line in srv.log.lines:
            self.status.append_log(line)
        self.settings_page.load(str(srv.data_dir), srv.host, srv.port, self.prefs.autostart, self.prefs.server_name)
        self._show_state()
        self.refresh_stats()
        self._timer.start()

    def _nav(self, key: str) -> None:
        self.pages.setCurrentIndex(NAV_INDEX[key])
        if key == "audit":
            self.audit.request()
        elif key == "users":
            self.users.reload()
        elif key == "status":
            self.refresh_stats()

    def _show_state(self, error: str = "", starting: bool = False) -> None:
        srv = self.server
        if srv is None:
            return
        state = "connecting" if starting else ("online" if (srv.running or self.service_active) else "offline")
        self.status.set_state(state, srv.urls(), str(srv.data_dir), srv.started_at, error,
                              service=self.service_active and not srv.running)
        self.sidebar.set_connection(state, f"{srv.host if srv.host != '0.0.0.0' else 'rete'}:{srv.port}")
        self.settings_page.load(str(srv.data_dir), srv.host, srv.port, self.prefs.autostart, self.prefs.server_name)
        self._refresh_tls_panel()
        if state == "online" and srv.insecure_warning:
            self.statusBar().showMessage(srv.insecure_warning, 15000)
        self.setWindowTitle(f"Automation Project Server - {'in esecuzione' if state == 'online' else 'fermo'}")

    def _refresh_tls_panel(self) -> None:
        srv = self.server
        if srv is None:
            return
        p = self.prefs
        cert = p.tls_certfile if p.tls_mode == "custom" else ""
        info = tlsmod.current_cert_info(srv.data_dir, cert)
        self.settings_page.load_tls(p.tls_enabled, p.tls_mode, p.tls_certfile, p.tls_keyfile, info.summary() if info else "")

    # ================================================================ server
    def start_server(self, then: Optional[Callable[[], None]] = None) -> None:
        srv = self.server
        if srv is None or srv.running:
            return
        self._show_state(starting=True)

        def ok(started: bool) -> None:
            self._show_state(error="" if started else srv.last_error)
            if not started:
                msg = srv.last_error or "Errore sconosciuto"
                if service_status() == "running":
                    msg = ("Il server e' gia' in esecuzione come servizio Windows: questa applicazione serve solo per "
                           "gestirlo (utenti, progetti, cronologia).")
                QMessageBox.warning(self, "Avvio non riuscito", msg)
            if then:
                then()
        self._run(srv.start, ok, lambda e: (self._show_state(error=str(e)), None))

    def stop_server(self, then: Optional[Callable[[], None]] = None) -> None:
        srv = self.server
        if srv is None:
            return

        def done(_r=None) -> None:                                                 # noqa: ANN001
            self._show_state()
            self.refresh_stats()
            if then:
                then()
        self._run(srv.stop, done, lambda e: done())

    def restart_server(self) -> None:
        self.stop_server(then=self.start_server)

    # ================================================================ statistiche
    def refresh_stats(self) -> None:
        srv = self.server
        if srv is None or srv.console is None:
            return
        api = srv.console
        self.status.set_clients(srv.connections())
        self.refresh_service_status()

        def work():
            return api.users(), api.projects(), api.audit(limit=12)

        def ok(res) -> None:
            users, projects, recent = res
            self.status.set_stats(sum(1 for u in users if u.is_active), sum(1 for p in projects if not p.archived),
                                  srv.connections())
            if self.service_active:
                self.status.st_clients[1].setText("-")              # i collegamenti li conta il servizio
            self.status.set_recent(recent)
            self.audit.set_project_names({p.id: p.name for p in projects})
        self._run(work, ok, lambda _e: None)

    def refresh_service_status(self) -> None:
        self._run(service_status, self._service_status_changed, lambda _e: None)

    def _service_status_changed(self, st: str) -> None:
        """Aggiorna il pannello e, se il servizio usa gli stessi dati, instrada la console verso di esso."""
        self.settings_page.set_service_status(st)
        srv = self.server
        if srv is None or srv.console is None:
            return
        url, pin = None, None
        if st == "running" and not srv.running:
            try:
                cfg = ServiceConfig.load()
                if Path(cfg.data_dir).resolve() == Path(srv.data_dir).resolve():
                    url = f"{tlsmod.scheme_for(cfg.tls_enabled)}://127.0.0.1:{cfg.port}"
                    if cfg.tls_enabled:                      # il servizio e' sul nostro stesso PC: se ne conosce l'impronta
                        info = tlsmod.current_cert_info(cfg.data_dir, cfg.tls_certfile)
                        pin = info.sha256 if info else None
            except (OSError, ValueError):
                url = None
        srv.console.use_remote(url, pin)
        if (url is not None) != self.service_active:
            self.service_active = url is not None
            self._show_state()

    # ================================================================ servizio Windows
    def service_action(self, action: str) -> None:
        """Installa / avvia / arresta / disinstalla il servizio Windows (richiede l'autorizzazione UAC)."""
        srv = self.server
        if srv is None:
            return
        args: list[str]
        if action == "install":
            r = QMessageBox.question(
                self, "Installa come servizio",
                f"Il server verra' installato come servizio Windows con questi dati:\n\n{srv.data_dir}\n"
                f"porta {srv.port}\n\nQuesta applicazione fermera' il proprio server (la porta serve al servizio) e "
                "l'avvio automatico dell'applicazione verra' disattivato. Windows chiedera' l'autorizzazione di amministratore.\n\n"
                "Nota: il servizio usa la cartella dati cosi' com'e': deve essere un percorso locale (non un'unita' di rete).")
            if r != QMessageBox.Yes:
                return
            args = ["install", "--data-dir", str(srv.data_dir), "--host", srv.host, "--port", str(srv.port),
                    "--name", srv.name or self.prefs.server_name]
            if srv.tls_enabled:                                  # le impostazioni TLS finiscono nel service.json; il certificato sta in <dati>/tls
                args.append("--tls")
                if srv.tls_certfile:
                    args += ["--tls-cert", srv.tls_certfile, "--tls-key", srv.tls_keyfile or srv.tls_certfile]
            else:
                args.append("--no-tls")
            self.prefs.autostart = False
        elif action in ("start", "stop", "remove"):
            args = [action]
        else:
            return
        if action in ("install", "start") and srv.running:
            self.stop_server()
        try:
            last_command_log().unlink()
        except OSError:
            pass
        if not run_elevated(*service_command(*args)):
            QMessageBox.warning(self, "Servizio", "Operazione annullata o non riuscita (serve l'autorizzazione di amministratore).")
            return
        self._svc_expect = {"install": ("running", "stopped", "pending"), "start": ("running",), "stop": ("stopped",),
                            "remove": ("not_installed",)}[action]
        self.statusBar().showMessage("Operazione sul servizio avviata: lo stato si aggiorna a breve", 8000)
        self._svc_ticks = 12
        QTimer.singleShot(1500, self._poll_service)

    def _poll_service(self) -> None:
        self._run(service_status, self._poll_result, lambda _e: None)

    def _poll_result(self, st: str) -> None:
        self._service_status_changed(st)
        self._svc_ticks = getattr(self, "_svc_ticks", 0) - 1
        if st in getattr(self, "_svc_expect", ()):
            self._svc_expect = ()
            return
        if self._svc_ticks > 0:
            QTimer.singleShot(2000, self._poll_service)
        elif getattr(self, "_svc_expect", ()):
            self._svc_expect = ()
            try:
                detail = last_command_log().read_text("utf-8", errors="replace").strip()[-800:]
            except OSError:
                detail = ""
            QMessageBox.warning(self, "Servizio", "L'operazione non risulta completata."
                                + (f"\n\nEsito:\n{detail}" if detail else ""))

    def _audit_query(self, f: dict) -> None:
        api = self.server.console
        self._run(lambda: api.audit(project_id=None, action=f["action"], actor=f["actor"]),
                  self.audit.set_entries, self._err)

    def _audit_verify(self) -> None:
        api = self.server.console
        self._run(api.audit_verify, self.audit.set_verify_result, self._err)

    def _err(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Errore", exc.detail if isinstance(exc, ApiError) else str(exc))

    # ================================================================ impostazioni
    def change_data_folder(self, dialog_factory: Callable[..., ChangeFolderDialog] = ChangeFolderDialog) -> None:
        srv = self.server
        if srv is None:
            return
        dlg = dialog_factory(srv.data_dir, self)
        if dlg.exec() != ChangeFolderDialog.Accepted:
            return
        base, copy = dlg.values()
        self.apply_data_folder(base, copy)

    def apply_data_folder(self, base: str, copy: bool) -> bool:
        """Ferma il server, (copia e) passa alla nuova cartella dati, riapre tutto e riavvia se era attivo."""
        old = self.server
        assert old is not None
        new_dir = resolve_data_dir(base)
        err = check_writable(new_dir)
        if err:
            QMessageBox.warning(self, "Cartella non utilizzabile", err)
            return False
        was_running = old.running
        old_dir = old.data_dir
        QGuiApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            old.close()                                  # ferma uvicorn e rilascia il database prima di copiare
            old.detach_logging()
            admin = None
            if copy and not has_data(new_dir):
                n = copy_data(old_dir, new_dir)
                old.log.lines.append(f"Copiati {n} file in {new_dir}")
            elif not has_data(new_dir):
                admin = ("admin", generate_password())
            self.prefs.data_base = base
        except Exception as exc:                                                   # noqa: BLE001
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Copia non riuscita", f"{exc}\n\nIl server restera' sulla cartella precedente.")
            self._open_server()
            return False
        QGuiApplication.restoreOverrideCursor()
        if not self._open_server(admin):
            self.prefs.data_base = str(old_dir)
            self._open_server()
            return False
        if admin:
            QMessageBox.information(self, "Nuova installazione",
                                    f"Utente: {admin[0]}\nPassword: {admin[1]}\n\nAnnotala: potrai cambiarla dalla pagina "
                                    "Utenti & Progetti.")
        self.statusBar().showMessage(f"Cartella dati: {new_dir}", 8000)
        if was_running:
            self.start_server()
        return True

    @staticmethod
    def _choose_backup_path(parent: QWidget, default_name: str) -> str:
        path, _ = QFileDialog.getSaveFileName(parent, "Crea backup dei dati del server", default_name, "Archivio zip (*.zip)")
        return path

    def create_backup(self, chooser: Optional[Callable[[QWidget, str], str]] = None) -> None:
        """Chiede dove salvare il backup (``chooser`` iniettabile nei test) e lo crea in un thread."""
        srv = self.server
        if srv is None:
            return
        path = (chooser or self._choose_backup_path)(self, default_backup_name())
        if not path:
            return
        dest = Path(path if path.lower().endswith(".zip") else path + ".zip")
        self.statusBar().showMessage("Creazione del backup in corso...")

        def ok(res: Path) -> None:
            size = res.stat().st_size / (1024 * 1024)
            self.statusBar().showMessage(f"Backup creato: {res} ({size:.1f} MB). Contiene il segreto JWT: conservalo "
                                         "in un posto protetto.", 15000)
        self._run(lambda: srv.backup(dest), ok, self._backup_error)

    def _backup_error(self, exc: Exception) -> None:
        self.statusBar().showMessage("Backup non riuscito", 8000)
        QMessageBox.warning(self, "Backup non riuscito", str(exc))

    def apply_network(self, host: str, port: int, name: Optional[str] = None, tls: Optional[dict] = None) -> None:
        """Applica rete e (se indicato) HTTPS: ``tls`` = ``{"enabled", "mode": "self"|"custom", "cert", "key"}``."""
        srv = self.server
        if srv is None:
            return
        if tls is not None and tls["enabled"] and tls["mode"] == "custom":     # un certificato errato non deve fermare un server funzionante
            try:
                if not tls["cert"]:
                    raise tlsmod.TlsError("Indica il file del certificato (.pem/.crt) e quello della chiave privata (.key).")
                tlsmod.validate_pair(tls["cert"], tls["key"] or None)
            except tlsmod.TlsError as exc:
                QMessageBox.warning(self, "Certificato non valido", str(exc))
                return
        was_running = srv.running
        srv.stop()
        srv.host, srv.port = host, port
        self.prefs.host, self.prefs.port = host, port
        if tls is not None:
            changed = (srv.tls_enabled, srv.tls_certfile) != (tls["enabled"], tls["cert"] if tls["mode"] == "custom" else "")
            self.prefs.tls_enabled, self.prefs.tls_mode = tls["enabled"], tls["mode"]
            self.prefs.tls_certfile, self.prefs.tls_keyfile = tls["cert"], tls["key"]
            kw = self._tls_kwargs()
            srv.tls_enabled, srv.tls_certfile, srv.tls_keyfile = kw["tls_enabled"], kw["tls_certfile"], kw["tls_keyfile"]
            if srv.app is not None:
                s = srv.app.state.settings
                s.tls_enabled, s.tls_certfile, s.tls_keyfile = srv.tls_enabled, srv.tls_certfile, srv.tls_keyfile
                if changed:
                    srv.app.state.audit.record("tls_settings_changed", "console", target="https" if srv.tls_enabled else "http",
                                               details={"enabled": srv.tls_enabled, "custom_certificate": bool(srv.tls_certfile)})
        if name is not None:
            self.prefs.server_name = srv.name = name
            if srv.app is not None:
                srv.app.state.settings.server_name = name
        self._show_state()
        if was_running:
            self.start_server()

    def generate_certificate(self, confirm: Optional[Callable[[], bool]] = None) -> None:
        """Crea (o rigenera) il certificato autofirmato in ``<dati>/tls``; chiede conferma se ne esiste gia' uno."""
        srv = self.server
        if srv is None:
            return
        cert, _key = tlsmod.default_paths(srv.data_dir)
        if cert.exists():
            ok = confirm() if confirm else QMessageBox.question(
                self, "Rigenera il certificato",
                "Esiste gia' un certificato. Se lo rigeneri, tutti i client che lo avevano accettato vedranno un "
                "avviso di certificato CAMBIATO e dovranno rimuovere la fiducia salvata e accettare il nuovo.\n\n"
                "Continuare?") == QMessageBox.Yes
            if not ok:
                return

        def ok_(info: "tlsmod.CertInfo") -> None:
            if srv.app is not None:
                srv.app.state.audit.record("tls_certificate_generated", "console", target=tlsmod.CERT_NAME,
                                           details={"sha256": info.sha256, "not_after": info.not_after.isoformat()})
            self._refresh_tls_panel()
            self.statusBar().showMessage("Certificato generato: premi «Applica e riavvia» per usarlo", 10000)
        self._run(lambda: tlsmod.generate_self_signed(srv.data_dir), ok_,
                  lambda e: QMessageBox.warning(self, "Certificato", f"Generazione non riuscita: {e}"))

    # ================================================================ chiusura
    def closeEvent(self, e) -> None:                                               # noqa: ANN001
        srv = self.server
        if srv is not None and srv.running and self._confirm_close:
            r = QMessageBox.question(self, "Chiudere il server?",
                                     "Il server e' in esecuzione: chiudendo l'applicazione i client collegati verranno "
                                     "disconnessi.\n\nVuoi arrestare il server e uscire?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self._timer.stop()
        if srv is not None:
            srv.close()
        e.accept()
