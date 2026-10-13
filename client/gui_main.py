"""Finestra principale del Automation Project Client: login -> workspace (sidebar + dashboard / cronologia / utenti)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QMainWindow,
                               QMessageBox, QStackedWidget, QSystemTrayIcon, QWidget)

from client import trust as _trust
from client.api_client import ApiClient, ApiError, TlsTrustError
from client.discovery import discover as discover_servers
from client.gui_approval import ApprovalDialog
import shutil
import threading

from client.auto_reimport import foreign_changes, manual_checkout_notice, merge_notice
from client.branch_info import abilities, display_name as branch_display_name
from client.localfiles import branch_folder, default_workdir, hide_ref_dir, norm_ext, prune_local, ref_path
from common.filekinds import DEFAULT_NAME, import_filters, rev_filename
from client.theme import apply_dark_theme
from client.theme.tokens import T
from client.theme.widgets import app_icon, icon
from client.views.audit_view import AuditView
from client.views.common import ScrollStack, fit_to_screen
from client.views.branches_dialog import BranchesDialog
from client.views.dashboard_view import DashboardView
from client.views.help_view import HelpView
from client.views.history_actions import HistoryActions
from client.views.history_dialog import MIN_REASON, HistoryDialog, revision_save_filter, revision_save_name
from client.views.common import preview_html
from client import updater
from common.version import APP_VERSION
from client.views.merge_dialog import MergeRequestDialog
from client.views.notifications_dialog import NotificationsDialog
from client.views.login_view import LoginView, PasswordChangeDialog, ask_forget_pin, ask_trust
from client.views.project_dialogs import NewProjectDialog
from client.views.sidebar import Sidebar
from client.views.user_mgmt_view import UserMgmtView
from client.workers import run_async
from client.ws_listener import WsListener
from common.roles import Role
from common.schemas import ProjectOut, ProposalState
from common.schemas_branches import BranchOut

HEARTBEAT_MS = 30_000
BRANCH_UNSUPPORTED = (404, 405, 501)          # server senza i branch: il selettore resta sul solo principale
NAV_INDEX = {"dashboard": 0, "audit": 1, "users": 2, "help": 3}


def _pick_folder_dialog(parent: QWidget, title: str, start: str) -> str:
    return QFileDialog.getExistingDirectory(parent, title, start)


def _pick_file_dialog(parent: QWidget, title: str, start: str, filters: str) -> str:
    return QFileDialog.getOpenFileName(parent, title, start, filters)[0]


_FILES_LOCK = threading.RLock()      # serializza le scritture su file del client (checkout, auto-sync, reimport)

def _file_sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MainWindow(QMainWindow):
    def __init__(self, run: Callable = run_async, settings: Optional[QSettings] = None,
                 heartbeat_ms: int = HEARTBEAT_MS, use_tray: bool = True,
                 discover: Callable = discover_servers) -> None:
        super().__init__()
        self._run = run
        self.pick_folder: Callable[[QWidget, str, str], str] = _pick_folder_dialog     # sostituibile nei test
        self.pick_file: Callable[[QWidget, str, str, str], str] = _pick_file_dialog    # (parent, titolo, cartella, filtri)
        # fiducia nei certificati HTTPS (pinning alla prima connessione): archivio e dialoghi sostituibili nei test
        self.trust = _trust.default_store()
        self.confirm_trust: Callable = ask_trust                    # (parent, host, port, CertSummary, motivo) -> bool
        self.confirm_forget_pin: Callable = ask_forget_pin          # (parent, host, port, messaggio) -> bool (True = rimuovi il pin)
        self.fetch_cert: Callable[[str, int], bytes] = _trust.fetch_server_cert
        self.settings = settings or QSettings("AutomationProjectServer", "Client2")
        self.setWindowTitle("Automation Project Client")
        self.setWindowIcon(app_icon())
        fit_to_screen(self, 1360, 840)
        self.api: Optional[ApiClient] = None
        self.project: Optional[ProjectOut] = None
        self.projects: list[ProjectOut] = []
        self.branches: list[BranchOut] = []                  # branch del progetto aperto (aperti e non)
        self.branches_dialog: Optional[BranchesDialog] = None
        self.dialogs: dict[str, ApprovalDialog] = {}
        self.ws = WsListener(self)
        self.ws.trust = self.trust
        self.ws.event_received.connect(self.on_event)
        self.ws.state_changed.connect(self._ws_state)
        self._hb = QTimer(self, interval=heartbeat_ms)
        self._hb.timeout.connect(self._heartbeat)

        self.stack = QStackedWidget()
        self.login = LoginView(servers=[self.settings.value("server_url", "http://localhost:8750")], run=run, discover=discover)
        self.login.trust = self.trust
        self.login.login_requested.connect(self._do_login)
        self.stack.addWidget(self.login)
        self.workspace_widget: Optional[QWidget] = None
        self.setCentralWidget(self.stack)

        self.tray: Optional[QSystemTrayIcon] = None
        if use_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.show()

    # ================================================================ avvio / login
    def start(self) -> None:
        self.login.prefill(self.settings.value("server_url", "http://localhost:8750"),
                           self.settings.value("username", ""))
        self.login.search_servers(auto=True)             # trova da solo i server in rete (o riusa l'ultimo usato)

    def _do_login(self, url: str, user: str, pw: str, _retried: bool = False) -> None:
        self.login.set_busy(True)
        api = ApiClient(url, trust=self.trust)

        def ok(_pair) -> None:
            self.login.set_busy(False)
            self.api = api
            if self.login.remember.isChecked():
                self.settings.setValue("server_url", url)
                self.settings.setValue("username", user)
            self.login.pw.clear()
            if api.user.must_change_password:
                self._force_password_change()
            else:
                self._enter_workspace()

        def fail(exc: Exception) -> None:
            if isinstance(exc, TlsTrustError) and self._tls_failed(exc, lambda: self._do_login(url, user, pw, True), _retried):
                return
            self.login.set_busy(False)
            self.login.set_error(exc.detail if isinstance(exc, ApiError) else str(exc))
        self._run(lambda: api.login(user, pw), ok, fail)

    def _tls_failed(self, exc: TlsTrustError, retry: Callable[[], None], retried: bool) -> bool:
        """Certificato non verificabile (prima connessione: chiede se fidarsi e salva l'impronta) o cambiato (rifiuta)."""
        host, port = exc.host, exc.port
        if exc.kind == "untrusted" and not retried:
            def got(der: bytes) -> None:
                summary = _trust.describe_cert(der)
                if self.confirm_trust(self, host, port, summary, exc.detail):
                    self.trust.pin(host, port, summary.sha256, summary, der)
                    retry()
                else:
                    self.login.set_busy(False)
                    self.login.set_error("Connessione annullata: certificato non considerato attendibile.")

            def unreadable(e: Exception) -> None:
                self.login.set_busy(False)
                self.login.set_error(f"Impossibile leggere il certificato del server: {e}")
            self._run(lambda: self.fetch_cert(host, port), got, unreadable)
            return True
        if exc.kind == "changed":
            self.login.set_busy(False)
            self.login.set_error("Il certificato del server e' cambiato: accesso bloccato per sicurezza.")
            if self.confirm_forget_pin(self, host, port, exc.detail):
                self.trust.remove(host, port)
                self.login.set_error("Fiducia rimossa: premi Accedi per vedere e verificare il nuovo certificato.")
            return True
        return False

    def _force_password_change(self, error: str = "") -> None:
        dlg = PasswordChangeDialog(forced=True, parent=self)
        if error:
            dlg.err.setText(error)
        if dlg.exec() != QDialog.Accepted:
            self._logout()
            return
        old, new = dlg.values()
        api = self.api

        def fail(exc: Exception) -> None:
            self._force_password_change(exc.detail if isinstance(exc, ApiError) else str(exc))
        self._run(lambda: api.change_password(old, new), lambda _x: self._enter_workspace(), fail)

    def change_password(self) -> None:
        dlg = PasswordChangeDialog(forced=False, parent=self)
        if dlg.exec() != QDialog.Accepted or not self.api:
            return
        old, new = dlg.values()
        api = self.api
        self._run(lambda: api.change_password(old, new),
                  lambda _x: self.statusBar().showMessage("Password aggiornata", 5000), self._err)

    # ================================================================ workspace
    def _enter_workspace(self) -> None:
        api = self.api
        assert api is not None and api.user is not None
        self.branches = []
        if self.workspace_widget is not None:
            self.stack.removeWidget(self.workspace_widget)
            self.workspace_widget.deleteLater()

        self.sidebar = Sidebar()
        self.dashboard = DashboardView()
        self.audit = AuditView()
        self.users = UserMgmtView(api, run=self._run)
        self.help = HelpView()
        self.pages = ScrollStack()
        for w in (self.dashboard, self.audit, self.users, self.help):
            self.pages.addWidget(w)
        if not hasattr(self, "_f1"):                                # F1: apre la Guida (una sola volta)
            self._f1 = QShortcut(QKeySequence(Qt.Key_F1), self)
            self._f1.activated.connect(self.show_help)
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.sidebar)
        lay.addWidget(self.pages, 1)
        self.workspace_widget = root
        self.stack.addWidget(root)

        self.sidebar.set_user(api.user)
        self.audit.set_role(api.role)
        self.sidebar.set_connection("connecting", api.base_url.split("://", 1)[-1])
        self.sidebar.nav_changed.connect(self._nav)
        self.sidebar.project_changed.connect(self._select_project)
        self.sidebar.set_can_create_project(api.role >= Role.ADMIN)         # creare un progetto e' riservato all'Admin
        self.sidebar.new_project_requested.connect(self.new_project)
        self.sidebar.logout_requested.connect(self._logout)
        self.sidebar.password_requested.connect(self.change_password)
        self.sidebar.notifications_requested.connect(lambda: self.show_notifications())
        self.dashboard.open_proposal.connect(self.open_proposal)
        self.dashboard.checkout_requested.connect(self.checkout)
        self.dashboard.merge_requested.connect(self.request_merge)
        self.dashboard.history_requested.connect(self.show_history)
        self.dashboard.import_master_requested.connect(self.import_master)
        self.dashboard.reimport_master_requested.connect(self.reimport_master)
        self.dashboard.refresh_requested.connect(self.refresh_all)
        self.dashboard.folder_change_requested.connect(self.choose_folder)
        self.dashboard.folder_open_requested.connect(self.open_folder)
        self.dashboard.lock_take_requested.connect(self.take_lock)
        self.dashboard.lock_release_requested.connect(self.release_lock)
        self.dashboard.lock_force_requested.connect(self.force_release_lock)
        self.dashboard.checkin_requested.connect(self.checkin_master)
        self.sidebar.branch_changed.connect(self.switch_branch)
        self.dashboard.branches_requested.connect(self.show_branches)
        self.dashboard.commit_requested.connect(lambda: self.commit_to_branch())
        self.dashboard.branch_sync_requested.connect(lambda: self.sync_branch())
        self.dashboard.branch_merge_requested.connect(lambda: self.merge_branch())
        self.dashboard.branch_leave_requested.connect(lambda: self.select_branch(None))
        self.audit.query_requested.connect(self._audit_query)
        self.audit.verify_requested.connect(self._audit_verify)
        self.users.projects_changed.connect(self.load_projects)
        self.users.status.connect(lambda m: self.statusBar().showMessage(m, 6000))

        self.stack.setCurrentWidget(root)
        self.setWindowTitle(f"Automation Project Client · {api.user.display_name or api.user.username}")
        self.dashboard.set_enabled_project(False)
        self.load_projects()
        self.refresh_unread()
        self.check_for_update()
        self._hb.start()

    def show_help(self, topic: str = "") -> None:
        """Apre la Guida (scorciatoia F1), opzionalmente su un argomento."""
        if self.workspace_widget is None or not hasattr(self, "help"):
            return
        self.sidebar.select("help")
        self._nav("help")
        if isinstance(topic, str) and topic:
            self.help.show_topic(topic)

    def _nav(self, key: str) -> None:
        self.pages.setCurrentIndex(NAV_INDEX[key])
        if key == "audit":
            self.audit.request()
        elif key == "users":
            self.users.reload()

    # ----------------------------------------------------------------- progetti
    @property
    def project_role(self) -> Role:
        if self.project and self.project.my_role:
            return Role.parse(self.project.my_role)
        return self.api.role if self.api else Role.VIEWER

    def load_projects(self) -> None:
        api = self.api
        if api is None:
            return
        self._run(api.projects, self._projects_loaded, self._err)

    def _projects_loaded(self, projects: list[ProjectOut]) -> None:
        self.projects = projects
        self.audit.set_project_names({p.id: p.name for p in projects})
        keep = self.project.id if self.project else int(self.settings.value("project_id", 0) or 0)
        cur = next((p for p in projects if p.id == keep), None) or next(
            (p for p in projects if not p.archived and p.head_revision is not None), None) or (
            projects[0] if projects else None)
        self.sidebar.set_projects(projects, cur.id if cur else None)
        if cur is None:
            self.project = None
            self.api.set_project(None)
            self._reset_branches()
            self.dashboard.set_master("Nessun progetto", None)
            self.dashboard.set_enabled_project(False)
            self.dashboard.set_proposals([])
            self.dashboard.set_local("Non hai accesso ad alcun progetto: chiedi a un amministratore di assegnartene uno.")
            self.ws.stop()
            return
        changed = self.project is None or self.project.id != cur.id
        self.project = cur
        if changed:
            self._select_project(cur.id)
        else:
            self._apply_project()

    def _select_project(self, pid: int) -> None:
        p = next((x for x in self.projects if x.id == pid), None)
        if p is None or self.api is None:
            return
        if self.project is None or self.project.id != pid:
            for d in list(self.dialogs.values()):
                d.close()
        keep_branch = self.api.branch if self.api.project_id == pid else None       # stesso progetto: resta nel suo branch
        self.project = p
        self.settings.setValue("project_id", pid)
        self.api.set_project(pid, keep_branch)
        if not keep_branch:
            self._reset_branches()
        self.dashboard.set_lock(None)
        self.dashboard.set_proposals([])
        self.dashboard.set_developers([])
        self._apply_project()
        self.ws.start(self.api.ws_url)
        self.refresh_all()

    def _apply_project(self) -> None:
        p = self.project
        if p is None:
            return
        self.dashboard.set_identity(self.api.session_key, self.project_role)
        self.dashboard.set_master_state(p.head_revision is not None)
        self.dashboard.set_master_kind(p.master_kind if p.head_revision is not None else None)
        self.dashboard.set_enabled_project(True)
        self.dashboard.set_branch(self.api.branch, self.branch)
        self.dashboard.set_merge_allowed(p.head_revision is not None and not p.archived)
        self.dashboard.btn_checkout.setEnabled(p.head_revision is not None)
        self._update_local_label()

    # ------------------------------------------------------------ stato locale
    def _folder_key(self) -> str:
        return f"folder/{self.api.base_url}/{self.project.id if self.project else 0}/{self.api.user.id}"

    @property
    def folder_chosen(self) -> bool:
        return bool(self.settings.value(self._folder_key(), ""))

    @property
    def project_folder(self) -> Path:
        """Cartella locale del progetto (principale): scelta dall'utente, altrimenti ``<workspace>/<utente>/<progetto>``."""
        chosen = self.settings.value(self._folder_key(), "")
        if chosen:
            return Path(str(chosen))
        root = self.settings.value("workspace", str(Path.home() / "AutomationProjectClient"))
        return default_workdir(root, self.api.user.username if self.api and self.api.user else "dev",
                               self.project.slug if self.project else "progetto")

    @property
    def local_folder(self) -> Path:
        """Cartella locale del contesto corrente: quella del progetto sul principale, ``<progetto>/branches/<slug>``
        su un branch (ognuna con il proprio ``.aps``)."""
        return branch_folder(self.project_folder, self.api.branch if self.api else None)

    workdir = local_folder                                       # nome storico

    @property
    def master_ext(self) -> str:
        """Estensione del Master del progetto corrente (.L5X, .L5K, o quella di qualsiasi altro file)."""
        return norm_ext(self.project.master_ext if self.project else None)

    @property
    def master_name(self) -> str:
        """Nome del file con cui e' stato importato il Master (senza estensione): i file scaricati lo mantengono."""
        return (self.project.master_name if self.project and self.project.master_name else DEFAULT_NAME)

    @property
    def master_kind(self) -> str:
        return (self.project.master_kind if self.project and self.project.master_kind else "l5x")

    def choose_folder(self) -> bool:
        """Chiede all'utente la cartella locale del progetto (dove salva il checkout e dove cerca i file da unire)."""
        if self.project is None or self.api is None:
            return False
        cur = self.project_folder                                   # i branch stanno in <cartella>/branches/<slug>
        start = str(cur if cur.is_dir() else (cur.parent if cur.parent.is_dir() else Path.home()))
        d = self.pick_folder(self, f"Cartella locale del progetto «{self.project.name}»", start)
        if not d:
            return False
        self.settings.setValue(self._folder_key(), str(Path(d)))
        self._update_local_label()
        self._log(f"Cartella locale: {d}")
        return True

    def open_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        f = self.local_folder
        f.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(f)))

    def _base_key(self) -> str:
        """Chiave delle impostazioni della copia di lavoro (revisione base, file): una per progetto e una per ogni branch."""
        key = f"base/{self.api.base_url}/{self.project.id if self.project else 0}/{self.api.user.id}"
        return f"{key}/b/{self.api.branch}" if self.api.branch else key

    # ----------------------------------------------------------------- branch (contesto)
    @property
    def branch(self) -> Optional[BranchOut]:
        """Descrizione del branch corrente (None sul principale o se l'elenco non e' ancora arrivato)."""
        slug = self.api.branch if self.api else None
        return next((b for b in self.branches if b.slug == slug), None) if slug else None

    @property
    def base_revision(self) -> int:
        return int(self.settings.value(self._base_key(), 0) or 0)

    @property
    def working_file(self) -> str:
        return str(self.settings.value(self._base_key() + "/file", "") or "")

    def _update_local_label(self) -> None:
        if self.project is None:
            return
        self.dashboard.set_folder(str(self.local_folder), self.folder_chosen)
        on_branch = bool(self.api and self.api.branch)
        if self.project_role < Role.DEVELOPER:
            self.dashboard.set_local("Accesso in sola lettura: puoi scaricare " + ("la testa del branch" if on_branch else "il Master")
                                     + " e consultare cronologia e storico.")
            return
        wf = self.working_file
        newer = self.newer_revision()
        self.dashboard.set_local(f"Copia di lavoro{' (branch)' if on_branch else ''}: rev base <b>{self.base_revision or '-'}</b>"
                                 + (f" &nbsp;|&nbsp; {wf}" if wf else "")
                                 + (f" &nbsp;|&nbsp; <span style='color:{T['ORANGE']}'><b>disponibile la rev {newer}</b>: "
                                    "fai Checkout per aggiornarla</span>" if newer else ""))

    def newer_revision(self) -> int:
        """Revisione piu' recente sul server (principale o branch corrente) se la tua copia di lavoro e' rimasta indietro,
        altrimenti 0 (anche se non hai ancora fatto il Checkout)."""
        base = self.base_revision
        if not base or self.api is None or self.project is None:
            return 0
        head = (self.branch.head_revision if self.branch else 0) if self.api.branch else (self.project.head_revision or 0)
        return head if head > base else 0

    # ----------------------------------------------------------------- messaggi
    def _log(self, msg: str) -> None:
        if self.workspace_widget is not None:
            self.dashboard.log_event(msg)
        self.statusBar().showMessage(msg, 8000)

    def _expired(self, exc: Exception) -> bool:
        if isinstance(exc, ApiError) and exc.status == 401 and self.api is not None and not self.api.logged_in:
            self._logout(expired=True)
            return True
        return False

    def _err(self, exc: Exception) -> None:
        if self._expired(exc):
            return
        QMessageBox.warning(self, "Errore", exc.detail if isinstance(exc, ApiError) else str(exc))

    def _quiet_err(self, exc: Exception) -> None:
        if self._expired(exc):
            return
        self.statusBar().showMessage(exc.detail if isinstance(exc, ApiError) else str(exc), 6000)

    # ================================================================ aggiornamento dati
    def _ws_state(self, state: str) -> None:
        if self.workspace_widget is not None and self.api is not None:
            self.sidebar.set_connection(state, self.api.base_url.split("://", 1)[-1])

    def _heartbeat(self) -> None:
        """Presenza (rende l'utente 'attivo' per il quorum) e, di passaggio, rinnovo del token."""
        api = self.api
        if api is None or not api.logged_in:
            return
        rev = self.base_revision or None
        if self.project is not None and self.project.head_revision is not None and self.project_role >= Role.DEVELOPER:
            self._run(lambda: api.report_revision(rev), None, self._quiet_err)
        else:
            self._run(api.me, None, self._quiet_err)
        if self.project is not None and self.project.head_revision is not None:          # presa in carico (lock)
            if self.dashboard.lock_is_mine():
                self._run(api.lock_renew, self._lock_loaded, self._lock_failed)         # rinnovo automatico dell'holder
            else:
                self.refresh_lock()

    def refresh_all(self) -> None:
        api, proj = self.api, self.project
        if api is None or proj is None:
            return
        name = proj.name
        live = self._same_context()                      # scarta le risposte arrivate dopo un cambio di progetto/branch

        def master_err(exc: Exception) -> None:
            if not live():
                return
            self.dashboard.set_master(name, None)
            if not (isinstance(exc, ApiError) and exc.status in (404, 409)):
                self._quiet_err(exc)

        def guarded(cb: Callable) -> Callable:
            return lambda res: cb(res) if live() else None
        self._run(api.master_info, guarded(lambda m: self.dashboard.set_master(name, m)), master_err)
        self._run(lambda: api.proposals(), guarded(self.dashboard.set_proposals), self._quiet_err)
        self._run(api.presence, guarded(self.dashboard.set_presence), self._quiet_err)
        self._run(api.projects, self._refresh_projects_meta, self._quiet_err)
        self.refresh_lock()
        self.load_branches()

    def _same_context(self) -> Callable[[], bool]:
        """Funzione che dice se progetto e branch sono ancora quelli di adesso (per le risposte asincrone)."""
        api = self.api
        pid, slug = (self.project.id if self.project else None), (api.branch if api else None)
        return lambda: (self.api is api and api is not None and self.project is not None and self.project.id == pid
                        and api.branch == slug)

    def _refresh_projects_meta(self, projects: list[ProjectOut]) -> None:
        """Aggiorna head_revision / policy senza ricostruire la selezione."""
        self.projects = projects
        if self.project is not None:
            fresh = next((p for p in projects if p.id == self.project.id), None)
            if fresh is not None:
                self.project = fresh
                self._apply_project()

    # ================================================================ azioni
    def checkout(self) -> None:
        api = self.api
        if api is None:
            return
        if not self.folder_chosen and not self.choose_folder():       # prima volta: l'utente decide dove salvare
            return
        ws, dev = self.local_folder, self.project_role >= Role.DEVELOPER
        self._run(lambda: self._download_working_copy(api, ws, dev),
                  lambda res: self._checkout_done(res, dev, "Checkout"), self._err)

    @staticmethod
    def _download_working_copy(api: ApiClient, ws: Path, dev: bool, revision: Optional[int] = None):
        with _FILES_LOCK:                                                   # _auto_sync e reimport non scrivono in parallelo
            return MainWindow._download_working_copy_locked(api, ws, dev, revision)

    @staticmethod
    def _download_working_copy_locked(api: ApiClient, ws: Path, dev: bool, revision: Optional[int] = None):
        """Parte di rete/disco del checkout (da eseguire fuori dal thread UI): scarica la revisione ``revision``
        (di default l'ultima) come ``<nome>_revN<ext>`` + copia pulita in ``.aps`` e segnala la revisione al server.
        Non sovrascrive mai un file di lavoro con modifiche non ancora unite. Ritorna ``(rev, dest, keep_mine)``."""
        info = api.master_info()
        rev = revision or info.revision
        ext, name = norm_ext(info.ext), info.name or DEFAULT_NAME
        dest = ws / rev_filename(name, rev, ext)                            # <nome del file importato>_revN.<ext>
        if dev:
            ref = ref_path(ws, name, rev, ext)                              # copia pulita (cartella nascosta .aps)
            keep_mine = dest.exists() and ref.exists() and dest.read_bytes() != ref.read_bytes()
            api.download_master(ref, rev)
            hide_ref_dir(ws)
            if not keep_mine:                                               # mai cancellare modifiche non ancora unite
                shutil.copyfile(ref, dest)
            api.report_revision(rev)
            return rev, dest, keep_mine
        api.download_master(dest, rev)
        return rev, dest, False

    def _checkout_done(self, res, dev: bool, what: str = "Checkout", announce: bool = True) -> None:
        """Parte UI del checkout: imposta revisione base e file di lavoro, aggiorna l'etichetta locale e i dati."""
        rev, dest, kept = res
        if dev:
            self.settings.setValue(self._base_key(), rev)
            self.settings.setValue(self._base_key() + "/file", str(dest))
            self._update_local_label()
            if announce:
                self._log(f"{what} Master rev {rev}: lavora su {dest}"
                          + (" (il file esisteva gia' con tue modifiche: non e' stato sovrascritto)" if kept else ""))
        elif announce:
            self._log(f"Master rev {rev} scaricato in {dest}")
        self._prune_local((dest,))
        self.refresh_all()

    def _prune_local(self, protect=()) -> None:
        """Pulizia locale: tiene sul PC al massimo ``project.max_revisions`` revisioni del Master (file
        ``<nome>_revN<ext>`` + copie in ``.aps``). Mai il file di lavoro, la sua base, file modificati o non
        riconosciuti: vedi ``localfiles.prune_local``. Gira dopo ogni download del Master."""
        api, proj = self.api, self.project
        if api is None or proj is None:
            return
        keep = int(getattr(proj, "max_revisions", 0) or 0)
        if keep < 1:
            return
        ws, name, ext, base = self.local_folder, self.master_name, self.master_ext, self.base_revision
        keep_paths = [str(p) for p in protect if p]
        if self.working_file:
            keep_paths.append(self.working_file)
        if base:
            keep_paths.append(str(ws / rev_filename(name, base, ext)))

        def work() -> list:
            with _FILES_LOCK:
                return prune_local(ws, name, ext, keep, keep_paths)

        def done(removed: list) -> None:
            n = len({p.name for p in removed})
            if n and self.api is api:
                self._log(f"Pulizia locale: rimosse {n} copie di vecchie revisioni")
        self._run(work, done, self._quiet_err)

    # ------------------------------------------------- nuovo progetto / file base (Master)
    def new_project(self) -> None:
        """Crea un progetto (solo Admin), lo seleziona e propone subito l'importazione del file base."""
        api = self.api
        if api is None or api.role < Role.ADMIN:
            return
        dlg = NewProjectDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.payload()

        def listed(created: ProjectOut):
            def ok(projects: list[ProjectOut]) -> None:
                self.projects = projects
                self.audit.set_project_names({p.id: p.name for p in projects})
                self.sidebar.set_projects(projects, created.id)
                self._select_project(created.id)
                self._offer_first_import(created)
            return ok

        def done(p: ProjectOut) -> None:
            self._log(f"Progetto «{p.name}» creato")
            self._run(api.projects, listed(p), self._err)
        self._run(lambda: api.create_project(data), done, self._err)

    def _offer_first_import(self, created: ProjectOut) -> None:
        if QMessageBox.question(self, "Importa file base",
                                f"Progetto «{created.name}» creato.\n\nVuoi importare subito il suo file base "
                                "(Master iniziale: L5K, L5X o qualsiasi altro file)?") == QMessageBox.Yes:
            self.import_master()

    def _pick_master_file(self, title: str) -> str:
        start = str(self.settings.value("import_dir", "") or Path.home())
        path = self.pick_file(self, title, start, import_filters())
        if path:
            self.settings.setValue("import_dir", str(Path(path).parent))
        return path or ""

    def import_master(self) -> None:
        """Master non ancora inizializzato: carica il file base (Lead o Admin del progetto)."""
        api, proj = self.api, self.project
        if api is None or proj is None or proj.head_revision is not None or self.project_role < Role.LEAD:
            return
        path = self._pick_master_file(f"File base di «{proj.name}» (qualsiasi file; Rockwell: .L5K / .L5X)")
        if not path:
            return

        def ok(m) -> None:
            self._log(f"File base importato in «{proj.name}»: Master rev {m.revision}")
            self.load_projects()
            self.refresh_all()
        self._run(lambda: api.init_master_for(proj.id, Path(path)), ok, self._err)

    def reimport_master(self) -> None:
        """Progetto in corso: crea una NUOVA revisione del Master da un file base aggiornato (Lead o Admin)."""
        api, proj = self.api, self.project
        if api is None or proj is None or proj.head_revision is None or self.project_role < Role.LEAD:
            return
        path = self._pick_master_file(f"Nuovo file base di «{proj.name}»")
        if not path:
            return
        if QMessageBox.question(
                self, "Reimporta file base",
                f"Dal file «{Path(path).name}» verrà creata una NUOVA revisione del Master di «{proj.name}».\n\n"
                "• la cronologia resta intatta (puoi sempre tornare alle revisioni precedenti);\n"
                "• le richieste di merge in corso vengono rianalizzate sul nuovo Master e i consensi già dati "
                "vengono azzerati.\n\nProcedere?") != QMessageBox.Yes:
            return
        text, ok_ = QInputDialog.getMultiLineText(
            self, "Reimporta file base", "Motivo della reimportazione (minimo 5 caratteri):")
        if not ok_:
            return
        reason = text.strip()
        if len(reason) < 5:
            QMessageBox.warning(self, "Motivo troppo breve", "Indica un motivo di almeno 5 caratteri.")
            return

        def done(m) -> None:
            self._log(f"File base reimportato in «{proj.name}»: nuova rev {m.revision} ({reason})")
            self.load_projects()
            self.refresh_all()
        self._run(lambda: api.reimport_master_for(proj.id, Path(path), reason), done, self._err)

    # ================================================================ presa in carico (lock) e check-in
    def refresh_lock(self) -> None:
        api = self.api
        if api is None or self.project is None or self.project.head_revision is None:
            return
        live = self._same_context()
        self._run(api.lock_state, lambda st: self._lock_loaded(st) if live() else None, self._quiet_err)

    def _lock_loaded(self, state) -> None:
        if self.workspace_widget is not None:
            self.dashboard.set_lock(state)

    def _lock_failed(self, exc: Exception) -> None:
        self._quiet_err(exc)
        self.refresh_lock()

    def take_lock(self, note: str = "") -> None:
        """«Prendi in carico»: segnala al team che stai lavorando sul Master (obbligatorio per il check-in dei binari)."""
        api = self.api
        if api is None or self.project is None or self.project_role < Role.DEVELOPER:
            return

        def done(st) -> None:
            self._lock_loaded(st)
            self._log("Hai preso in carico il Master")

        def fail(exc: Exception) -> None:
            self._err(exc)
            self.refresh_lock()
        self._run(lambda: api.lock_acquire(note), done, fail)

    def release_lock(self) -> None:
        api = self.api
        if api is None or self.project is None:
            return

        def done(st) -> None:
            self._lock_loaded(st)
            self._log("Hai rilasciato il Master")
        self._run(api.lock_release, done, self._lock_failed_err)

    def force_release_lock(self, reason: Optional[str] = None) -> None:
        """«Forza rilascio» (solo Admin): libera il Master preso in carico da un altro, con motivo (tracciato nell'audit)."""
        api = self.api
        if api is None or self.project is None or self.project_role < Role.ADMIN:
            return
        if not isinstance(reason, str):
            who = self.dashboard.lock_holder()
            reason, ok = QInputDialog.getMultiLineText(
                self, "Forza rilascio", f"Il Master è in carico a {who or 'un altro utente'}: verrà liberato subito.\n"
                                        "Motivo (minimo 5 caratteri, registrato nell'audit):")
            if not ok:
                return
        reason = reason.strip()
        if len(reason) < MIN_REASON:
            QMessageBox.warning(self, "Motivo troppo breve", f"Indica un motivo di almeno {MIN_REASON} caratteri.")
            return

        def done(st) -> None:
            self._lock_loaded(st)
            self._log("Presa in carico forzata: il Master è libero")
        self._run(lambda: api.lock_release(reason), done, self._lock_failed_err)

    def checkin_master(self, path: Optional[str] = None, message: Optional[str] = None) -> None:
        """Check-in diretto di una nuova revisione di un Master BINARIO (serve la presa in carico). ``path``/``message``
        saltano i dialoghi (test)."""
        api, proj = self.api, self.project
        if api is None or proj is None or proj.head_revision is None or self.project_role < Role.DEVELOPER:
            return
        if self.master_kind != "binary":
            QMessageBox.information(self, "Check-in", "Il check-in diretto è riservato ai file binari: per questo "
                                                      "progetto usa «Richiedi Merge».")
            return
        if not self.dashboard.lock_is_mine():
            QMessageBox.information(self, "Check-in", "Prima prendi in carico il Master («Prendi in carico»).")
            return
        if not isinstance(path, str):
            path = self._pick_master_file(f"Nuova versione del file di «{proj.name}»")
        if not path:
            return
        if not isinstance(message, str):
            message, ok_ = QInputDialog.getMultiLineText(
                self, "Check-in nuova revisione", f"Messaggio della revisione (minimo {MIN_REASON} caratteri):")
            if not ok_:
                return
        message = message.strip()
        if len(message) < MIN_REASON:
            QMessageBox.warning(self, "Messaggio troppo breve", f"Indica un messaggio di almeno {MIN_REASON} caratteri.")
            return

        def done(m) -> None:
            self._log(f"Check-in eseguito in «{proj.name}»: nuova rev {m.revision}")
            self.load_projects()
            self.refresh_all()
        self._run(lambda: api.checkin_master(Path(path), message), done, self._lock_failed_err)

    def _lock_failed_err(self, exc: Exception) -> None:
        self._err(exc)
        self.refresh_lock()

    def _on_lock_changed(self, ev: dict) -> None:
        """Evento ``lock_changed``: aggiorna la barra e, se e' stato un altro, avvisa senza bloccare."""
        self.refresh_lock()
        who = ev.get("developer")
        if who and self.api is not None and self.api.user is not None and who != self.api.user.username:
            self.notify_user("Presa in carico", ev.get("message", ""))

    def request_merge(self) -> None:
        holder = self.dashboard.lock_holder()
        if holder and not self.dashboard.lock_is_mine() and self.master_kind != "binary":
            self._log(f"Attenzione: il Master è in carico a {holder} (avviso consultivo: la richiesta di merge è possibile)")
        if self.base_revision == 0:
            QMessageBox.information(self, "Checkout necessario", "Esegui prima il Checkout dell'ultimo Master.")
            return
        api, base = self.api, self.base_revision
        dlg = MergeRequestDialog(self.local_folder, self.working_file, self.base_revision,
                                 self.project.name if self.project else "", parent=self,
                                 ext=self.master_ext, kind=self.master_kind, name=self.master_name,
                                 check_fn=lambda p: api.dry_run(p, base), run=self._run)
        if dlg.exec() != QDialog.Accepted or dlg.selected_path() is None:
            return
        self.submit_merge(dlg.selected_path(), dlg.message_text())

    def submit_merge(self, path: Path, message: str) -> None:
        api, base = self.api, self.base_revision

        def done(d) -> None:
            self._log(f"Richiesta {d.id} inviata: {d.n_changes} modifiche, {d.n_conflicts} conflitti")
            self.refresh_all()
            self.open_proposal(d.id)
        self._run(lambda: api.submit_proposal(path, base, message), done, self._err)

    # ================================================================ branch
    def _reset_branches(self) -> None:
        self.branches = []
        if self.workspace_widget is not None and hasattr(self, "sidebar"):
            self.sidebar.set_branches([], self.api.branch if self.api else None)

    def load_branches(self) -> None:
        """Ricarica l'elenco dei branch del progetto aperto (selettore, banner, «da main rev N, M indietro»)."""
        api, proj = self.api, self.project
        if api is None or proj is None or proj.head_revision is None or self.workspace_widget is None:
            return
        pid = proj.id

        def ok(bs: list[BranchOut]) -> None:
            if self.api is api and self.project is not None and self.project.id == pid:
                self._branches_loaded(bs)

        def fail(exc: Exception) -> None:
            if isinstance(exc, ApiError) and exc.status in BRANCH_UNSUPPORTED:
                return                                          # server senza i branch: solo il principale
            self._quiet_err(exc)
        self._run(lambda: api.branches(pid), ok, fail)

    def _branches_loaded(self, bs: list[BranchOut]) -> None:
        self.branches = list(bs)
        cur = self.api.branch
        if cur and not any(b.slug == cur for b in bs):          # eliminato da un altro utente
            self._log(f"Il branch «{cur}» non esiste più: torno al principale")
            self.select_branch(None)
            return
        self.sidebar.set_branches(self.branches, cur)
        self._apply_project()

    def switch_branch(self, slug: Optional[str]) -> None:
        """Cambio di branch voluto dall'utente (selettore, elenco branch): oltre a cambiare contesto scarica subito la testa
        del branch nella sua cartella locale se li' non c'e' ancora una copia di lavoro (altrimenti la cartella resterebbe vuota
        fino al primo Checkout)."""
        self.select_branch(slug)
        if slug and self.api is not None and self.api.branch == slug and self.project is not None \
                and self.project.head_revision is not None and self.base_revision == 0:
            self.checkout()

    def select_branch(self, slug: Optional[str]) -> None:
        """Cambia contesto: ``None`` / ``""`` = principale, altrimenti il branch ``slug``. Imposta ``api.branch``,
        chiude le finestre di consenso aperte, ricarica dashboard / cronologia / richieste / lock e riconnette il
        WebSocket al nuovo branch."""
        api, proj = self.api, self.project
        if api is None or proj is None:
            return
        slug = slug or None
        if slug == api.branch:
            self.sidebar.select_branch(slug)
            return
        for d in list(self.dialogs.values()):
            d.close()
        api.set_project(proj.id, slug)
        self.dashboard.set_lock(None)
        self.dashboard.set_proposals([])
        self.dashboard.set_developers([])
        self.sidebar.set_branches(self.branches, slug)
        self._apply_project()
        self._log(f"Ora lavori sul branch «{branch_display_name(self.branch, slug)}»" if slug else "Ora lavori sul principale")
        self.ws.start(api.ws_url)                               # sottoscrizione agli eventi del nuovo branch
        self.refresh_all()

    def show_branches(self) -> Optional[BranchesDialog]:
        """Apre l'elenco dei branch (nuovo, passa, aggiorna dal principale, unisci nel principale, chiudi, elimina)."""
        api, proj = self.api, self.project
        if api is None or proj is None or proj.head_revision is None:
            return None
        dlg = BranchesDialog(api, proj, self.project_role, api.branch, self._run, self)
        dlg.switch_requested.connect(self.switch_branch)
        dlg.sync_requested.connect(lambda s: self.sync_branch(s))
        dlg.merge_requested.connect(lambda s: self.merge_branch(s))
        dlg.preview_requested.connect(lambda s, m: self.preview_branch(s, m))
        dlg.changed.connect(self.load_branches)
        dlg.created_branch.connect(self.on_branch_created)
        self.branches_dialog = dlg
        dlg.exec()
        return dlg

    def on_branch_created(self, slug: str) -> None:
        """Dopo «Nuovo branch»: passa al branch e scarica subito la sua copia di lavoro, cosi' sul PC compare la cartella
        ``<cartella progetto>/branches/<slug>/`` con l'export della revisione da cui parte (e la copia pulita in ``.aps``)."""
        if self.api is None or self.project is None:
            return
        self.select_branch(slug)
        if self.api.branch != slug:
            return
        if self.project.head_revision is None:
            return
        self.checkout()
        self._log(f"Branch creato: cartella locale {self.local_folder}")

    def _branch_by_slug(self, slug: str) -> Optional[BranchOut]:
        return next((b for b in self.branches if b.slug == slug), None)

    def preview_branch(self, slug: str, mode: str = "merge", show: bool = True):
        """Anteprima (dry-run) dell'unione nel principale (``mode='merge'``) o dell'aggiornamento dal principale
        (``'sync'``) di un branch: non crea nessuna richiesta. Mostra l'esito in una finestra (``show=False`` nei test)."""
        api = self.api
        if api is None or self.project is None:
            return None
        b = self._branch_by_slug(slug)
        label = branch_display_name(b, slug)
        what = "l'unione nel principale" if mode == "merge" else "l'aggiornamento dal principale"

        def done(p) -> None:
            self.last_preview = p
            self._log(f"Anteprima {what} del branch «{label}»: "
                      + ("nessuna differenza" if p.nothing_to_merge else f"{p.n_changes} modifiche, {p.n_conflicts} conflitti"))
            if show:
                QMessageBox.information(self, f"Anteprima · {label}", f"<b>{label}</b> — {what}<br><br>" + preview_html(p, what))
        self._run(lambda: api.branch_dry_run(slug, mode), done, self._err)

    last_preview = None                # ultima anteprima ricevuta (utile ai test)

    def sync_branch(self, slug: Optional[str] = None) -> None:
        """«Aggiorna dal principale»: richiesta di merge DENTRO il branch con le novita' del principale. Si resta
        nel branch e si apre la proposta."""
        api = self.api
        slug = slug or (api.branch if api else None)
        if api is None or self.project is None or not slug or self.project_role < Role.DEVELOPER:
            return
        b = self._branch_by_slug(slug)
        if b is not None and not abilities(b, self.project_role).sync:
            self._log(f"Branch «{b.name}»: nessun aggiornamento da portare dal principale (o branch di sola lettura)")
            return
        label = branch_display_name(b, slug)

        def done(d) -> None:
            self._log(f"Aggiornamento dal principale: richiesta {d.id} nel branch «{label}» "
                      f"({d.n_changes} modifiche, {d.n_conflicts} conflitti)")
            if api.branch != slug:
                self.select_branch(slug)
            self.refresh_all()
            self.open_proposal(d.id)
        self._run(lambda: api.branch_sync(slug), done, self._err)

    def merge_branch(self, slug: Optional[str] = None, message: Optional[str] = None, path: Optional[str] = None) -> None:
        """«Unisci nel principale»: come «Richiedi Merge» si sceglie il file da inviare (di solito il file di lavoro del
        branch). Se il file e' diverso dalla testa del branch viene prima salvato nel branch (branch personale tuo;
        in un branch team il file viene inviato direttamente nella richiesta di merge nel principale), poi parte la richiesta di merge del branch
        NEL principale (consenso normale), si passa al principale e si apre la proposta. ``message`` salta i dialoghi
        (test); ``path`` e' il file scelto (None = la testa del branch, senza salvare niente)."""
        api = self.api
        slug = slug or (api.branch if api else None)
        if api is None or self.project is None or not slug or self.project_role < Role.DEVELOPER:
            return
        b = self._branch_by_slug(slug)
        if b is not None and not abilities(b, self.project_role).merge:
            self._log(f"Branch «{b.name}»: non puoi unirlo nel principale (sola lettura)")
            return
        label = branch_display_name(b, slug)
        if not isinstance(message, str):
            if api.branch != slug:
                self.select_branch(slug)                         # i file candidati sono quelli della cartella del branch
            dlg = MergeRequestDialog(self.local_folder, self.working_file, self.base_revision,
                                     f"{self.project.name} › {label}", parent=self, ext=self.master_ext,
                                     kind=self.master_kind, name=self.master_name, title="Unisci nel principale",
                                     heading_text=f"Quale file vuoi unire nel principale dal branch «{label}»?",
                                     ok_text="Unisci nel principale", run=self._run,
                                     check_fn=lambda p: api.branch_dry_run(slug, "merge", p))
            if dlg.exec() != QDialog.Accepted or dlg.selected_path() is None:
                return
            path = str(dlg.selected_path())
            message = dlg.message_text()
        message = (message or "").strip()
        can_commit = b is not None and b.kind == "personal" and abilities(b, self.project_role).commit   # nei team il file va dentro la richiesta
        ws, name, ext = self.local_folder, self.master_name, self.master_ext
        base_key = self._base_key()

        def work():
            saved = None
            if path:
                info = api.master_info()                          # testa del branch
                if _file_sha(Path(path)) != info.sha256:
                    if not can_commit:                                # branch team: il file viene inviato direttamente nel merge
                        return None, api.branch_merge(slug, message, Path(path)), "merge"
                    saved = api.branch_commit(slug, Path(path), message or "Modifiche prima dell'unione nel principale")
                    try:
                        with _FILES_LOCK:
                            api.download_master(ref_path(ws, name, saved.revision, ext), saved.revision)
                            hide_ref_dir(ws)
                        api.report_revision(saved.revision)
                    except Exception:                             # noqa: BLE001 - il salvataggio e' gia' avvenuto
                        pass
            return saved, api.branch_merge(slug, message), "merge"

        def done(res) -> None:
            saved, d, _kind = res
            if saved is not None:
                self._log(f"Salvato nel branch «{label}»: nuova rev {saved.revision}")
                self.settings.setValue(base_key, saved.revision)
                self.settings.setValue(base_key + "/file", str(path))
            self._log(f"Unione nel principale: richiesta {d.id} per il branch «{label}» "
                      f"({d.n_changes} modifiche, {d.n_conflicts} conflitti)")
            self.select_branch(None)                            # la proposta vive nel principale
            self.refresh_all()
            self.open_proposal(d.id)
        self._run(work, done, self._err)

    def commit_to_branch(self, path: Optional[str] = None, message: Optional[str] = None) -> None:
        """«Salva su branch...»: salvataggio diretto nel branch: nel TUO branch personale una nuova revisione; in un branch
        team le tue modifiche si uniscono (3-way) a quelle gia' presenti, senza votazione. ``path`` / ``message`` saltano i dialoghi (test)."""
        api, proj, b = self.api, self.project, self.branch
        if api is None or proj is None or b is None or not abilities(b, self.project_role).commit:
            return
        slug = b.slug
        if not isinstance(path, str):
            dlg = MergeRequestDialog(self.local_folder, self.working_file, self.base_revision, f"{proj.name} › {b.name}",
                                     parent=self, ext=self.master_ext, kind=self.master_kind, name=self.master_name,
                                     title="Salva su branch", heading_text=f"Quale file vuoi salvare nel branch «{b.name}»?",
                                     ok_text="Salva nel branch")
            if dlg.exec() != QDialog.Accepted or dlg.selected_path() is None:
                return
            path = str(dlg.selected_path())
            if not isinstance(message, str):
                message = dlg.message_text()
        message = (message or "").strip()
        if not message:
            QMessageBox.warning(self, "Messaggio mancante", "Descrivi brevemente cosa stai salvando nel branch.")
            return
        ws, name, ext = self.local_folder, self.master_name, self.master_ext
        base_key = self._base_key()

        team = b.kind == "team"
        base_rev = self.base_revision or None

        def fetch_head(rev: int) -> None:                       # copia pulita della nuova testa del branch (nel .aps)
            try:
                with _FILES_LOCK:
                    api.download_master(ref_path(ws, name, rev, ext), rev)
                    hide_ref_dir(ws)
                api.report_revision(rev)
            except Exception:                                   # noqa: BLE001 - il salvataggio e' gia' avvenuto
                pass

        def work():
            if team:                                            # unione 3-way con quanto gia' nel branch, consenso automatico
                d = api.branch_save(slug, Path(path), message, base_rev)
                if d.state == ProposalState.MERGED and d.result_revision is not None:
                    fetch_head(d.result_revision)
                return d
            m = api.branch_commit(slug, Path(path), message)
            fetch_head(m.revision)
            return m

        def done(m) -> None:
            if team:
                if m.state != ProposalState.MERGED or m.result_revision is None:
                    self._log(f"Salvataggio nel branch «{b.name}»: ci sono {m.n_conflicts} conflitti con le modifiche già presenti, da risolvere")
                    self.refresh_all()
                    self.open_proposal(m.id)
                    return
                rev = m.result_revision
                self._log(f"Salvato nel branch «{b.name}»: nuova rev {rev}")
                if self.api is api:
                    ref = ref_path(ws, name, rev, ext)
                    same = False
                    try:
                        same = ref.exists() and ref.read_bytes() == Path(path).read_bytes()
                    except OSError:
                        pass
                    if same:                                    # nessun altro aveva toccato il branch: il tuo file e' la nuova testa
                        self.settings.setValue(base_key, rev)
                        self.settings.setValue(base_key + "/file", str(path))
                    else:                                       # il branch contiene anche modifiche dei colleghi
                        self._log("Il branch contiene anche modifiche dei colleghi: fai il Checkout per aggiornare il tuo file")
                    self._update_local_label()
                    self._prune_local((path,))
                    self.refresh_all()
                return
            self._log(f"Salvato nel branch «{b.name}»: nuova rev {m.revision}")
            if self.api is api:
                self.settings.setValue(base_key, m.revision)             # il file salvato e' ora la copia di lavoro
                self.settings.setValue(base_key + "/file", str(path))
                self._update_local_label()
                self._prune_local((path,))
                self.refresh_all()
        self._run(work, done, self._err)

    def show_history(self) -> None:
        api = self.api
        if api is None:
            return

        def show(history) -> None:
            dlg = HistoryDialog(history, self.project_role, self,
                                max_revisions=self.project.max_revisions if self.project else None)
            if api.branch:
                dlg.setWindowTitle(f"Storico revisioni del branch «{branch_display_name(self.branch, api.branch)}»")
            self.history_dialog = dlg
            dlg.download_requested.connect(lambda rev: self._download_revision(rev))
            dlg.restore_requested.connect(lambda rev: self._restore(rev, dlg))
            HistoryActions(dlg, api, self._run, on_status=self._log, on_error=self._err)     # confronta / etichetta / blocca
            dlg.exec()
        self._run(api.history, show, self._err)

    def _download_revision(self, rev: int, path: Optional[str] = None) -> None:
        """Salva una revisione del Master (non cancella ne' sposta nulla dal PC). ``path`` serve ai test."""
        ext = self.master_ext
        if not isinstance(path, str):
            path, _ = QFileDialog.getSaveFileName(self, "Salva revisione",
                                                  revision_save_name(self.master_name, rev, ext),
                                                  revision_save_filter(ext))
        if path:
            api = self.api

            def done(_x) -> None:
                self._log(f"Master rev {rev} salvato in {path}")
                self._prune_local((path,))                       # il file appena salvato non si tocca mai
            self._run(lambda: api.download_master(Path(path), rev), done, self._err)

    def _restore(self, rev: int, parent: QWidget, reason: Optional[str] = None) -> None:
        """Rollback (solo Admin): la revisione ``rev`` diventa una NUOVA revisione del Master. ``reason`` salta la
        domanda (test). Le revisioni eliminate dal server (limite) non si possono ripristinare."""
        api = self.api
        if api is None or self.project_role < Role.ADMIN:
            return
        if not isinstance(reason, str):
            reason, ok = QInputDialog.getMultiLineText(
                parent, "Ripristina revisione",
                f"Verrà creata una nuova revisione con il contenuto della rev {rev}; la cronologia resta intatta.\n"
                f"Motivo (minimo {MIN_REASON} caratteri):")
            if not ok:
                return
        reason = reason.strip()
        if len(reason) < MIN_REASON:
            QMessageBox.warning(self, "Motivo troppo breve", f"Indica un motivo di almeno {MIN_REASON} caratteri.")
            return

        def done(m) -> None:
            self._log(f"Master ripristinato dalla rev {rev}: nuova rev {m.revision}")
            self.load_projects()
            self.refresh_all()
            if isinstance(parent, HistoryDialog):                             # lo storico aperto mostra la nuova rev
                def refill(h) -> None:
                    try:
                        parent.set_history(h)
                    except RuntimeError:                                      # dialogo gia' chiuso
                        pass
                self._run(api.history, refill, self._quiet_err)
        self._run(lambda: api.restore(rev, reason), done, self._err)

    def open_proposal(self, pid: str) -> None:
        if self.api is None:
            return
        dlg = self.dialogs.get(pid)
        if dlg is None:
            dlg = ApprovalDialog(self.api, pid, my_role=self.project_role, run=self._run, parent=self,
                                 master_ext=self.master_ext, master_name=self.master_name)
            if self.api.branch:
                dlg.setWindowTitle(f"Richiesta di Merge · Consenso · branch «{branch_display_name(self.branch, self.api.branch)}»")
            dlg.setAttribute(Qt.WA_DeleteOnClose)
            dlg.destroyed.connect(lambda *_a, p=pid: self.dialogs.pop(p, None))
            dlg.proposal_changed.connect(lambda _d: self.refresh_all())
            self.dialogs[pid] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # ----------------------------------------------------------------- audit
    def _audit_query(self, f: dict) -> None:
        api, proj = self.api, self.project
        if api is None:
            return
        pid = None if (f["all_projects"] or proj is None) else proj.id
        self._run(lambda: api.audit(project_id=pid, action=f["action"], actor=f["actor"]),
                  self.audit.set_entries, self._err)

    def _audit_verify(self) -> None:
        api = self.api
        self._run(api.audit_verify, self.audit.set_verify_result, self._err)

    # ================================================================ eventi push
    def on_event(self, ev: dict) -> None:
        if self.api is None:
            return
        t, msg, prop = ev.get("type"), ev.get("message", ""), ev.get("proposal")
        if t == "hello":
            return
        if t == "notification":                                    # nuova notifica persistente per me
            self._on_notification(ev)
            return
        if t == "project_deleted":
            self._on_project_deleted(ev)
            return
        if t == "main_updated" or (t and str(t).startswith("branch_")):
            # il principale e' avanzato (arriva ai client dei branch) oppure un branch e' stato creato / aggiornato /
            # chiuso / unito / eliminato: cambiano «indietro» e stati, non i dati della vista corrente
            self._log(msg)
            self.load_branches()
            return
        if "branch" in ev and t != "project_updated":
            ev_branch = ev.get("branch") or None                   # None / "" = principale
            if ev_branch != self.api.branch:                       # evento di un altro branch: non riguarda questa vista
                if t in ("master_updated", "proposal_merged"):     # ...ma puo' cambiare «indietro» dei branch
                    self.load_branches()
                return
        self._log(msg)
        if t == "lock_changed":                                    # presa in carico / rilascio del Master
            self._on_lock_changed(ev)
            return
        if t == "project_updated":                                 # nome / retention / policy cambiati dall'Admin
            self.load_projects()
            self.refresh_all()
            return
        if t in ("developer_joined", "developer_left"):
            self._run(self.api.presence, self.dashboard.set_presence, self._quiet_err)
            return
        if self.tray and t in ("proposal_submitted", "proposal_rejected", "proposal_merged", "master_updated"):
            self.tray.showMessage("Automation Project", msg)
        if t == "proposal_submitted" and prop and prop.get("author_session") != self.api.session_key:
            mine = any(p.get("session_id") == self.api.session_key for p in prop.get("participants", []))
            if mine and self.project_role >= Role.DEVELOPER:
                self.open_proposal(prop["id"])                     # schermata di consenso per chi deve approvare
        if t == "comment_added":
            self._on_comment_added(ev)                             # notifica non bloccante (partecipante / @menzione)
        for dlg in list(self.dialogs.values()):
            dlg.on_event(ev)
        if t == "proposal_merged" and prop:
            self._on_merged(prop)                                  # mio merge con modifiche altrui -> reimport automatico
        if t == "master_updated" and ev.get("revision") is not None:
            self._auto_sync(int(ev["revision"]))
            if self.api.branch:                                    # qualcuno ha salvato nel branch su cui lavori
                self._on_branch_new_revision(int(ev["revision"]), msg)
        self.refresh_all()

    def _on_branch_new_revision(self, rev: int, msg: str = "", delay_ms: int = 1500,
                                ask: Optional[Callable[[str], bool]] = None) -> None:
        """Un'altra persona ha salvato la rev ``rev`` nel branch in cui lavori: avvisa e propone di scaricarla (Checkout, che non
        sovrascrive mai un file con modifiche tue). Dopo ``delay_ms`` si ricontrolla, cosi' un mio stesso salvataggio (che
        aggiorna la revisione base) non genera l'avviso. ``ask`` sostituisce il dialogo (test)."""
        slug = self.api.branch if self.api else None

        def check() -> None:
            if self.api is None or self.api.branch != slug or self.project_role < Role.DEVELOPER:
                return
            base = self.base_revision
            if not base or rev <= base:
                return
            self._update_local_label()
            text = (f"{msg or 'Nuova revisione nel branch'}.\n\nLa tua copia di lavoro è basata sulla rev {base}: "
                    f"vuoi scaricare ora la rev {rev}? (il tuo file di lavoro con modifiche non verrà sovrascritto)")
            if ask is not None:
                if ask(text):
                    self.checkout()
                return
            box = QMessageBox(QMessageBox.Question, "Nuova revisione nel branch", text, QMessageBox.Yes | QMessageBox.No, self)
            box.button(QMessageBox.Yes).setText("Scarica ora")
            box.button(QMessageBox.No).setText("Più tardi")
            box.setAttribute(Qt.WA_DeleteOnClose)
            box.setWindowModality(Qt.NonModal)
            box.finished.connect(lambda r: self.checkout() if r == QMessageBox.Yes else None)
            box.show()
        if delay_ms <= 0:
            check()
        else:
            QTimer.singleShot(delay_ms, check)

    # ----------------------------------------------------------------- aggiornamento automatico del client
    def check_for_update(self, ask: Optional[Callable[[str], bool]] = None) -> None:
        """All'accesso chiede al server se esiste una versione piu' recente del client; se sì (e si usa l'eseguibile) propone
        di aggiornare: scarica, verifica lo SHA-256, chiude l'app e la riavvia sostituita. ``ask`` sostituisce il dialogo (test)."""
        api = self.api
        if api is None:
            return

        def found(info) -> None:
            if not info.available:
                return
            self.update_info = info
            frozen = updater.running_exe() is not None
            self._log(f"Disponibile una nuova versione del client: {info.version} (installata: {APP_VERSION})")
            if not frozen:
                return                                             # da sorgenti non c'e' un eseguibile da sostituire
            text = (f"È disponibile la versione {info.version} del client (tu hai la {APP_VERSION}).\n\n"
                    + (f"{info.notes}\n\n" if info.notes else "") + "Vuoi aggiornare ora? L'applicazione verrà riavviata.")
            yes = ask(text) if ask is not None else QMessageBox.question(self, "Aggiornamento disponibile", text) == QMessageBox.Yes
            if yes:
                self.install_update(info)
        self._run(lambda: updater.check(api), found, self._quiet_err)

    update_info = None

    def install_update(self, info, quit_app: bool = True) -> None:
        api = self.api
        if api is None:
            return
        self._log(f"Scarico il client {info.version}...")

        def work():
            return updater.download(api, info)

        def done(path) -> None:
            try:
                updater.apply(path)
            except updater.UpdateError as exc:
                QMessageBox.warning(self, "Aggiornamento non riuscito", str(exc))
                return
            self._log("Aggiornamento pronto: riavvio dell'applicazione")
            if quit_app:
                QApplication.quit()
        self._run(work, done, self._err)

    # ----------------------------------------------------------------- notifiche (posta in arrivo)
    def refresh_unread(self) -> None:
        api = self.api
        if api is None:
            return
        self._run(api.notifications_unread, lambda n: self.sidebar.set_unread(n), self._quiet_err)

    def _on_notification(self, ev: dict) -> None:
        self.sidebar.set_unread(int(ev.get("revision") or 0))
        msg = ev.get("message", "")
        self._log(f"Notifica: {msg}")
        if self.tray:
            self.tray.showMessage("Automation Project", msg)

    def show_notifications(self) -> Optional[NotificationsDialog]:
        if self.api is None:
            return None
        dlg = NotificationsDialog(self.api, self._run, self)
        dlg.unread_changed.connect(lambda n: self.sidebar.set_unread(n))
        dlg.open_requested.connect(self.open_notification)
        self.notifications_dialog = dlg
        dlg.exec()
        self.refresh_unread()
        return dlg

    notifications_dialog = None

    def open_notification(self, n) -> None:                        # noqa: ANN001  (NotificationOut)
        """Apre la richiesta (o il branch) collegati a una notifica, passando prima al progetto / branch giusti."""
        if self.api is None:
            return
        if n.project_id and (self.project is None or self.project.id != n.project_id):
            self._select_project(n.project_id)
        if n.project_id:
            self.select_branch(n.branch or None)
        if n.ref:
            self.open_proposal(n.ref)

    def _on_comment_added(self, ev: dict) -> None:
        """Nuovo commento su una richiesta: avviso non bloccante se ne sono autore/partecipante o se sono @menzionato.
        Nessun avviso per i miei stessi commenti; se la finestra della richiesta e' gia' aperta basta quella (salvo menzione)."""
        api = self.api
        if api is None or api.user is None:
            return
        me, who = api.user.username, ev.get("developer") or ""
        if who == me:
            return
        prop = ev.get("proposal") or {}
        mentioned = me in (ev.get("mentions") or [])
        involved = bool(prop) and (prop.get("author_session") == api.session_key or any(
            x.get("session_id") == api.session_key for x in prop.get("participants", [])))
        if not (mentioned or involved):
            return
        dlg = self.dialogs.get(prop.get("id", ""))
        if not mentioned and dlg is not None and dlg.isVisible():
            return
        if mentioned:
            self._notify_all("Sei stato menzionato", f"{who} ti ha menzionato nella discussione della richiesta "
                                                     f"#{prop.get('id', '')}.")
        else:
            self._notify_all("Nuovo commento", ev.get("message", ""))

    def _auto_sync(self, rev: int) -> None:
        """Distribuzione automatica: scarica il nuovo Master senza toccare la copia di lavoro."""
        api, ws = self.api, self.workdir

        mine = Path(self.working_file).resolve() if self.working_file else None

        def work() -> Path:
            with _FILES_LOCK:
                return work_locked()

        def work_locked() -> Path:
            info = api.master_info()
            ext, name = norm_ext(info.ext), info.name or DEFAULT_NAME
            dest = ws / rev_filename(name, rev, ext)
            if self.project_role >= Role.DEVELOPER:
                api.download_master(ref_path(ws, name, rev, ext), rev)         # copia pulita di riferimento
                hide_ref_dir(ws)
                if mine is None or dest.resolve() != mine:                       # mai sovrascrivere il file su cui lavori
                    shutil.copyfile(ref_path(ws, name, rev, ext), dest)
            else:
                api.download_master(dest, rev)
            return dest

        def ok(dest) -> None:
            if self.project_role >= Role.DEVELOPER:
                self._log(f"Master rev {rev} scaricato in {dest} (la tua copia di lavoro resta basata su "
                          f"rev {self.base_revision}: fai Checkout per ripartire dal nuovo Master)")
            else:
                self._log(f"Master rev {rev} scaricato in {dest}")
            self._prune_local((dest,))
        self._run(work, ok, self._quiet_err)

    # ------------------------------------------------------------ progetto eliminato dall'Admin
    def _on_project_deleted(self, ev: dict) -> None:
        """Evento ``project_deleted``: il progetto aperto non esiste piu' sul server. Si ferma tutto cio' che lo
        riguarda (WebSocket, finestre di consenso), lo si toglie dall'elenco e si passa a un altro progetto o allo
        stato «nessun progetto». I file locali dell'utente NON vengono toccati."""
        if self.api is None:
            return
        gone = ev.get("project_id", ev.get("project"))
        cur = self.project
        if cur is None or (gone is not None and int(gone) != cur.id):
            self.load_projects()                                   # un altro progetto: basta aggiornare l'elenco
            return
        name, folder = cur.name, self.project_folder
        self.ws.stop()                                            # niente piu' eventi/riconnessioni del progetto morto
        for d in list(self.dialogs.values()):
            d.close()
        self.projects = [p for p in self.projects if p.id != cur.id]
        self.project = None
        self.api.set_project(None)
        if int(self.settings.value("project_id", 0) or 0) == cur.id:
            self.settings.setValue("project_id", 0)
        self.dashboard.set_proposals([])
        self.dashboard.set_developers([])
        self._projects_loaded(self.projects)                       # altro progetto disponibile oppure «nessun progetto»
        self.load_projects()                                       # e riallineamento con il server
        self._notify_all("Progetto eliminato",
                         f"Il progetto «{name}» è stato eliminato dal server. I tuoi file locali non sono stati "
                         f"toccati: restano in {folder}.")

    # ------------------------------------------------------------ reimport automatico dopo il mio merge
    def notify_user(self, title: str, text: str) -> None:
        """Avviso visibile ma NON bloccante (finestra non modale). Sostituibile nei test."""
        box = QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, self)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setWindowModality(Qt.NonModal)
        box.show()

    def _notify_all(self, title: str, text: str) -> None:
        """Log + barra di stato + tray (se c'e') + avviso nella finestra."""
        self._log(f"{title}: " + " ".join(text.split()))
        if self.tray:
            self.tray.showMessage(title, text)
        self.notify_user(title, text)

    def _on_merged(self, prop: dict) -> None:
        """Evento ``proposal_merged``: se l'autore sono io (anche merge forzato) guarda se il server ha trovato
        modifiche sul Master che non avevo; solo in quel caso scarica in automatico il nuovo Master."""
        api = self.api
        pid = prop.get("id")
        if api is None or self.project is None or not pid or prop.get("author_session") != api.session_key:
            return
        if self.project_role < Role.DEVELOPER:
            return
        handled = self.__dict__.setdefault("_handled_merges", set())
        if pid in handled:                                             # evento ripetuto / gia' in corso
            return
        handled.add(pid)
        project_id, ws = self.project.id, self.local_folder
        result_rev = prop.get("result_revision")

        def failed(exc: Exception) -> None:
            handled.discard(pid)                                       # potra' essere ritentato da un nuovo evento
            self._quiet_err(exc)

        def got(detail) -> None:
            foreign = foreign_changes(detail)
            if not foreign:
                self._log(f"Merge {pid} completato: nessuna modifica altrui da riportare, copia di lavoro invariata")
                return
            rev = detail.result_revision if detail.result_revision is not None else result_rev
            if self.api is not api or self.project is None or self.project.id != project_id:
                return                                                 # nel frattempo logout / cambio progetto

            def downloaded(res) -> None:
                if self.api is not api or self.project is None or self.project.id != project_id:
                    return
                new_rev, dest, kept = res
                self._checkout_done(res, True, "Reimport automatico", announce=False)
                self._notify_all("Merge completato: copia di lavoro aggiornata",
                                 merge_notice(rev, foreign, dest.name, new_rev, kept))

            def download_failed(exc: Exception) -> None:
                self._quiet_err(exc)
                self._notify_all("Merge completato: esegui il Checkout",
                                 manual_checkout_notice(rev, foreign, exc.detail if isinstance(exc, ApiError) else str(exc)))
            self._run(lambda: self._download_working_copy(api, ws, True, rev), downloaded, download_failed)
        self._run(lambda: api.proposal(pid), got, failed)

    # ================================================================ logout / chiusura
    def _logout(self, expired: bool = False) -> None:
        self._hb.stop()
        self.ws.stop()
        for d in list(self.dialogs.values()):
            d.close()
        api, self.api, self.project, self.projects = self.api, None, None, []
        if api is not None and api.logged_in:
            self._run(api.logout, None, lambda _e: None)
        if self.workspace_widget is not None:
            self.stack.setCurrentWidget(self.login)
            self.stack.removeWidget(self.workspace_widget)
            self.workspace_widget.deleteLater()
            self.workspace_widget = None
        self.setWindowTitle("Automation Project Client")
        self.login.set_busy(False)
        self.login.set_error("Sessione scaduta o revocata: effettua di nuovo il login." if expired else "")
        self.login.pw.clear()
        self.login.check_server()

    def closeEvent(self, e) -> None:                                  # noqa: ANN001
        self._hb.stop()
        self.ws.stop()
        if self.api is not None and self.api.logged_in:
            try:
                self.api.logout()
            except Exception:                                         # noqa: BLE001 - chiusura best-effort
                pass
        super().closeEvent(e)


def main() -> None:
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("Automation Project Client")
    app.setWindowIcon(app_icon())
    apply_dark_theme(app)
    w = MainWindow()
    w.show()
    w.start()
    sys.exit(app.exec())
