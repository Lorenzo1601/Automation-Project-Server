"""Finestra principale del Automation Project Client: login -> workspace (sidebar + dashboard / cronologia / utenti)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QMainWindow,
                               QMessageBox, QStackedWidget, QSystemTrayIcon, QWidget)

from client.api_client import ApiClient, ApiError
from client.discovery import discover as discover_servers
from client.gui_approval import ApprovalDialog
import shutil
import threading

from client.auto_reimport import foreign_changes, manual_checkout_notice, merge_notice
from client.localfiles import default_workdir, hide_ref_dir, norm_ext, ref_path
from common.filekinds import DEFAULT_NAME, import_filters, rev_filename
from client.theme import apply_dark_theme
from client.theme.tokens import T
from client.theme.widgets import app_icon, icon
from client.views.audit_view import AuditView
from client.views.dashboard_view import DashboardView, HistoryDialog
from client.views.merge_dialog import MergeRequestDialog
from client.views.login_view import LoginView, PasswordChangeDialog
from client.views.project_dialogs import NewProjectDialog
from client.views.sidebar import Sidebar
from client.views.user_mgmt_view import UserMgmtView
from client.workers import run_async
from client.ws_listener import WsListener
from common.roles import Role
from common.schemas import ProjectOut

HEARTBEAT_MS = 30_000
NAV_INDEX = {"dashboard": 0, "audit": 1, "users": 2}


def _pick_folder_dialog(parent: QWidget, title: str, start: str) -> str:
    return QFileDialog.getExistingDirectory(parent, title, start)


def _pick_file_dialog(parent: QWidget, title: str, start: str, filters: str) -> str:
    return QFileDialog.getOpenFileName(parent, title, start, filters)[0]


_FILES_LOCK = threading.RLock()      # serializza le scritture su file del client (checkout, auto-sync, reimport)

class MainWindow(QMainWindow):
    def __init__(self, run: Callable = run_async, settings: Optional[QSettings] = None,
                 heartbeat_ms: int = HEARTBEAT_MS, use_tray: bool = True,
                 discover: Callable = discover_servers) -> None:
        super().__init__()
        self._run = run
        self.pick_folder: Callable[[QWidget, str, str], str] = _pick_folder_dialog     # sostituibile nei test
        self.pick_file: Callable[[QWidget, str, str, str], str] = _pick_file_dialog    # (parent, titolo, cartella, filtri)
        self.settings = settings or QSettings("AutomationProjectServer", "Client2")
        self.setWindowTitle("Automation Project Client")
        self.setWindowIcon(app_icon())
        self.resize(1360, 840)
        self.api: Optional[ApiClient] = None
        self.project: Optional[ProjectOut] = None
        self.projects: list[ProjectOut] = []
        self.dialogs: dict[str, ApprovalDialog] = {}
        self.ws = WsListener(self)
        self.ws.event_received.connect(self.on_event)
        self.ws.state_changed.connect(self._ws_state)
        self._hb = QTimer(self, interval=heartbeat_ms)
        self._hb.timeout.connect(self._heartbeat)

        self.stack = QStackedWidget()
        self.login = LoginView(servers=[self.settings.value("server_url", "http://localhost:8750")], run=run, discover=discover)
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

    def _do_login(self, url: str, user: str, pw: str) -> None:
        self.login.set_busy(True)
        api = ApiClient(url)

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
            self.login.set_busy(False)
            self.login.set_error(exc.detail if isinstance(exc, ApiError) else str(exc))
        self._run(lambda: api.login(user, pw), ok, fail)

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
        if self.workspace_widget is not None:
            self.stack.removeWidget(self.workspace_widget)
            self.workspace_widget.deleteLater()

        self.sidebar = Sidebar()
        self.dashboard = DashboardView()
        self.audit = AuditView()
        self.users = UserMgmtView(api, run=self._run)
        self.pages = QStackedWidget()
        for w in (self.dashboard, self.audit, self.users):
            self.pages.addWidget(w)
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
        self.dashboard.open_proposal.connect(self.open_proposal)
        self.dashboard.checkout_requested.connect(self.checkout)
        self.dashboard.merge_requested.connect(self.request_merge)
        self.dashboard.history_requested.connect(self.show_history)
        self.dashboard.import_master_requested.connect(self.import_master)
        self.dashboard.reimport_master_requested.connect(self.reimport_master)
        self.dashboard.refresh_requested.connect(self.refresh_all)
        self.dashboard.folder_change_requested.connect(self.choose_folder)
        self.dashboard.folder_open_requested.connect(self.open_folder)
        self.audit.query_requested.connect(self._audit_query)
        self.audit.verify_requested.connect(self._audit_verify)
        self.users.projects_changed.connect(self.load_projects)
        self.users.status.connect(lambda m: self.statusBar().showMessage(m, 6000))

        self.stack.setCurrentWidget(root)
        self.setWindowTitle(f"Automation Project Client · {api.user.display_name or api.user.username}")
        self.dashboard.set_enabled_project(False)
        self.load_projects()
        self._hb.start()

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
        self.project = p
        self.settings.setValue("project_id", pid)
        self.api.set_project(pid)
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
        self.dashboard.set_enabled_project(True)
        self.dashboard.btn_merge.setEnabled(p.head_revision is not None and not p.archived)
        self.dashboard.btn_checkout.setEnabled(p.head_revision is not None)
        self._update_local_label()

    # ------------------------------------------------------------ stato locale
    def _folder_key(self) -> str:
        return f"folder/{self.api.base_url}/{self.project.id if self.project else 0}/{self.api.user.id}"

    @property
    def folder_chosen(self) -> bool:
        return bool(self.settings.value(self._folder_key(), ""))

    @property
    def local_folder(self) -> Path:
        """Cartella locale del progetto: scelta dall'utente, altrimenti ``<workspace>/<utente>/<progetto>``."""
        chosen = self.settings.value(self._folder_key(), "")
        if chosen:
            return Path(str(chosen))
        root = self.settings.value("workspace", str(Path.home() / "AutomationProjectClient"))
        return default_workdir(root, self.api.user.username if self.api and self.api.user else "dev",
                               self.project.slug if self.project else "progetto")

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
        cur = self.local_folder
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
        return f"base/{self.api.base_url}/{self.project.id if self.project else 0}/{self.api.user.id}"

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
        if self.project_role < Role.DEVELOPER:
            self.dashboard.set_local("Accesso in sola lettura: puoi scaricare il Master e consultare cronologia e storico.")
            return
        wf = self.working_file
        self.dashboard.set_local(f"Copia di lavoro: rev base <b>{self.base_revision or '-'}</b>"
                                 + (f" &nbsp;|&nbsp; {wf}" if wf else ""))

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

    def refresh_all(self) -> None:
        api, proj = self.api, self.project
        if api is None or proj is None:
            return
        name = proj.name

        def master_err(exc: Exception) -> None:
            self.dashboard.set_master(name, None)
            if not (isinstance(exc, ApiError) and exc.status in (404, 409)):
                self._quiet_err(exc)
        self._run(api.master_info, lambda m: self.dashboard.set_master(name, m), master_err)
        self._run(lambda: api.proposals(), self.dashboard.set_proposals, self._quiet_err)
        self._run(api.developers, self.dashboard.set_developers, self._quiet_err)
        self._run(api.projects, self._refresh_projects_meta, self._quiet_err)

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
        self.refresh_all()

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

    def request_merge(self) -> None:
        if self.base_revision == 0:
            QMessageBox.information(self, "Checkout necessario", "Esegui prima il Checkout dell'ultimo Master.")
            return
        dlg = MergeRequestDialog(self.local_folder, self.working_file, self.base_revision,
                                 self.project.name if self.project else "", parent=self,
                                 ext=self.master_ext, kind=self.master_kind, name=self.master_name)
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

    def show_history(self) -> None:
        api = self.api
        if api is None:
            return

        def show(history) -> None:
            dlg = HistoryDialog(history, self.project_role, self)
            dlg.download_requested.connect(lambda rev: self._download_revision(rev))
            dlg.restore_requested.connect(lambda rev: self._restore(rev, dlg))
            dlg.exec()
        self._run(api.history, show, self._err)

    def _download_revision(self, rev: int) -> None:
        ext = self.master_ext
        path, _ = QFileDialog.getSaveFileName(self, "Salva revisione", rev_filename(self.master_name, rev, ext),
                                              f"File del progetto (*{ext});;Tutti i file (*)")
        if path:
            api = self.api
            self._run(lambda: api.download_master(Path(path), rev),
                      lambda _x: self._log(f"Master rev {rev} salvato in {path}"), self._err)

    def _restore(self, rev: int, parent: QWidget) -> None:
        text, ok = QInputDialog.getMultiLineText(
            parent, "Ripristina revisione",
            f"Verrà creata una nuova revisione con il contenuto della rev {rev}; la cronologia resta intatta.\n"
            "Motivo (minimo 3 caratteri):")
        if not ok or len(text.strip()) < 3:
            return
        api = self.api

        def done(m) -> None:
            self._log(f"Master ripristinato dalla rev {rev}: nuova rev {m.revision}")
            self.refresh_all()
        self._run(lambda: api.restore(rev, text.strip()), done, self._err)

    def open_proposal(self, pid: str) -> None:
        if self.api is None:
            return
        dlg = self.dialogs.get(pid)
        if dlg is None:
            dlg = ApprovalDialog(self.api, pid, my_role=self.project_role, run=self._run, parent=self,
                                 master_ext=self.master_ext, master_name=self.master_name)
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
        self._log(msg)
        if t in ("developer_joined", "developer_left"):
            self._run(self.api.developers, self.dashboard.set_developers, self._quiet_err)
            return
        if self.tray and t in ("proposal_submitted", "proposal_rejected", "proposal_merged", "master_updated"):
            self.tray.showMessage("Automation Project", msg)
        if t == "proposal_submitted" and prop and prop.get("author_session") != self.api.session_key:
            mine = any(p.get("session_id") == self.api.session_key for p in prop.get("participants", []))
            if mine and self.project_role >= Role.DEVELOPER:
                self.open_proposal(prop["id"])                     # schermata di consenso per chi deve approvare
        for dlg in list(self.dialogs.values()):
            dlg.on_event(ev)
        if t == "proposal_merged" and prop:
            self._on_merged(prop)                                  # mio merge con modifiche altrui -> reimport automatico
        if t == "master_updated" and ev.get("revision") is not None:
            self._auto_sync(int(ev["revision"]))
        self.refresh_all()

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
        self._run(work, ok, self._quiet_err)

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
