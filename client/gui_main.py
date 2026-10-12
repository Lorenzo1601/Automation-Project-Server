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
from client.localfiles import default_workdir, master_copy_name, norm_ext
from client.theme import apply_dark_theme
from client.theme.tokens import T
from client.theme.widgets import app_icon, icon
from client.views.audit_view import AuditView
from client.views.dashboard_view import DashboardView, HistoryDialog
from client.views.merge_dialog import MergeRequestDialog
from client.views.login_view import LoginView, PasswordChangeDialog
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


class MainWindow(QMainWindow):
    def __init__(self, run: Callable = run_async, settings: Optional[QSettings] = None,
                 heartbeat_ms: int = HEARTBEAT_MS, use_tray: bool = True,
                 discover: Callable = discover_servers) -> None:
        super().__init__()
        self._run = run
        self.pick_folder: Callable[[QWidget, str, str], str] = _pick_folder_dialog     # sostituibile nei test
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
        self.sidebar.logout_requested.connect(self._logout)
        self.sidebar.password_requested.connect(self.change_password)
        self.dashboard.open_proposal.connect(self.open_proposal)
        self.dashboard.checkout_requested.connect(self.checkout)
        self.dashboard.merge_requested.connect(self.request_merge)
        self.dashboard.history_requested.connect(self.show_history)
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

        def work():
            info = api.master_info()
            ext = norm_ext(info.ext)
            if dev:
                dest = ws / f"Lavoro_rev{info.revision}{ext}"
                api.download_master(dest, info.revision)
                api.download_master(ws / master_copy_name(ext), info.revision)
                api.report_revision(info.revision)
            else:
                dest = ws / f"Master_rev{info.revision}{ext}"
                api.download_master(dest, info.revision)
            return info.revision, dest

        def ok(res) -> None:
            rev, dest = res
            if dev:
                self.settings.setValue(self._base_key(), rev)
                self.settings.setValue(self._base_key() + "/file", str(dest))
                self._update_local_label()
                self._log(f"Checkout Master rev {rev}: lavora su {dest}")
            else:
                self._log(f"Master rev {rev} scaricato in {dest}")
            self.refresh_all()
        self._run(work, ok, self._err)

    def request_merge(self) -> None:
        if self.base_revision == 0:
            QMessageBox.information(self, "Checkout necessario", "Esegui prima il Checkout dell'ultimo Master.")
            return
        dlg = MergeRequestDialog(self.local_folder, self.working_file, self.base_revision,
                                 self.project.name if self.project else "", parent=self,
                                 ext=self.master_ext, kind=self.master_kind)
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
        path, _ = QFileDialog.getSaveFileName(self, "Salva revisione", f"Master_rev{rev}{ext}",
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
                                 master_ext=self.master_ext)
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
        if t == "master_updated" and ev.get("revision") is not None:
            self._auto_sync(int(ev["revision"]))
        self.refresh_all()

    def _auto_sync(self, rev: int) -> None:
        """Distribuzione automatica: scarica il nuovo Master senza toccare la copia di lavoro."""
        api, ws = self.api, self.workdir

        def work() -> int:
            ext = norm_ext(api.master_info().ext)
            api.download_master(ws / master_copy_name(ext), rev)
            api.download_master(ws / f"Master_rev{rev}{ext}", rev)
            return rev

        def ok(_r) -> None:
            if self.project_role >= Role.DEVELOPER:
                self._log(f"Master rev {rev} scaricato in {ws / master_copy_name(self.master_ext)} (la tua copia di lavoro resta basata su "
                          f"rev {self.base_revision}: fai Checkout per ripartire dal nuovo Master)")
            else:
                self._log(f"Master rev {rev} scaricato in {ws / master_copy_name(self.master_ext)}")
        self._run(work, ok, self._quiet_err)

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
