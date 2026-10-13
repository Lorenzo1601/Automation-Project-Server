"""Gestione utenti, permessi di progetto, sessioni e ruoli (solo Admin)."""
from __future__ import annotations

import re
import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from client.api_client import ApiClient, ApiError
from client.theme.tokens import T
from client.theme.widgets import Avatar, icon, role_color
from client.views.branches_dialog import BranchesDialog
from client.views.history_actions import HistoryActions
from client.views.history_dialog import MIN_REASON, HistoryDialog, revision_save_filter, revision_save_name
from client.views.project_dialogs import DeleteProjectDialog, EditProjectDialog, NewProjectDialog   # noqa: F401
from client.views.common import chip, full_time, h_line, heading, item, make_table, muted, role_chip, role_label
from client.workers import run_async
from common.filekinds import import_filters
from common.roles import ROLE_DESCRIPTIONS, ROLE_KEYS, ROLE_LABELS, Role
from common.schemas import (PermissionOut, ProjectCreate, ProjectOut, ProjectUpdate, UserCreate, UserOut,
                            UserUpdate)

NO_ACCESS = "—"


def suggest_password(n: int = 14) -> str:
    """Password casuale conforme alla policy del server (>= 10 caratteri, almeno una lettera e una cifra)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _role_combo(max_role: Role = Role.ADMIN, allow_none: bool = False, include_admin: bool = True) -> QComboBox:
    cb = QComboBox()
    if allow_none:
        cb.addItem("Nessun accesso", None)
    for r in sorted(Role, reverse=True):
        if r > max_role or (r == Role.ADMIN and not include_admin):
            continue
        cb.addItem(ROLE_LABELS[r], r.key)
    return cb


# ======================================================================================= dialog utente
class UserDialog(QDialog):
    """Creazione (``user=None``) o modifica di un account."""

    def __init__(self, user: Optional[UserOut] = None, self_id: Optional[int] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.user = user
        self.setWindowTitle("Nuovo utente" if user is None else f"Modifica {user.username}")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        lay.addWidget(heading("Nuovo utente" if user is None else user.username, 2))
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft)
        self.username = QLineEdit(user.username if user else "")
        self.username.setPlaceholderText("nome.cognome")
        self.username.setEnabled(user is None)
        self.display = QLineEdit(user.display_name if user else "")
        self.email = QLineEdit(user.email if user else "")
        self.role = _role_combo()
        if user:
            i = self.role.findData(user.role)
            self.role.setCurrentIndex(max(0, i))
            if user.id == self_id:
                self.role.setEnabled(False)              # il server vieta l'auto-declassamento
                self.role.setToolTip("Non puoi cambiare il tuo stesso ruolo")
        else:
            self.role.setCurrentIndex(self.role.findData("Developer"))
        form.addRow("Utente", self.username)
        form.addRow("Nome visualizzato", self.display)
        form.addRow("Email", self.email)
        form.addRow("Ruolo massimo", self.role)

        pw_row = QHBoxLayout()
        self.password = QLineEdit()
        self.password.setPlaceholderText("min. 10 caratteri, lettere e cifre" if user is None
                                         else "lascia vuoto per non cambiarla")
        gen = QPushButton("Genera")
        gen.setProperty("variant", "ghost")
        gen.clicked.connect(self._generate)
        pw_row.addWidget(self.password, 1)
        pw_row.addWidget(gen)
        form.addRow("Password" if user is None else "Reset password", pw_row)
        lay.addLayout(form)
        self.must_change = QCheckBox("Richiedi il cambio password al prossimo accesso")
        self.must_change.setChecked(True)
        lay.addWidget(self.must_change)
        self.active = QCheckBox("Account attivo")
        self.active.setChecked(user.is_active if user else True)
        self.active.setVisible(user is not None)
        if user and user.id == self_id:
            self.active.setEnabled(False)
        lay.addWidget(self.active)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color:{T['RED']};")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _generate(self) -> None:
        self.password.setText(suggest_password())
        self.password.setEchoMode(QLineEdit.Normal)

    def _accept(self) -> None:
        if self.user is None:
            if len(self.username.text().strip()) < 3:
                self.error.setText("Utente: almeno 3 caratteri.")
                return
            if not self.password.text():
                self.error.setText("Imposta una password iniziale (puoi generarla).")
                return
        if not self.display.text().strip():
            self.error.setText("Indica il nome visualizzato.")
            return
        self.accept()

    def create_payload(self) -> UserCreate:
        return UserCreate(username=self.username.text().strip(), display_name=self.display.text().strip(),
                          email=self.email.text().strip(), password=self.password.text(),
                          role=self.role.currentData(), must_change_password=self.must_change.isChecked())

    def update_payload(self) -> UserUpdate:
        u = self.user
        assert u is not None
        pw = self.password.text()
        return UserUpdate(
            display_name=self.display.text().strip() if self.display.text().strip() != u.display_name else None,
            email=self.email.text().strip() if self.email.text().strip() != u.email else None,
            role=self.role.currentData() if self.role.currentData() != u.role else None,
            is_active=self.active.isChecked() if self.active.isChecked() != u.is_active else None,
            new_password=pw or None,
            must_change_password=self.must_change.isChecked() if pw else None)


# ======================================================================================= vista
class UserMgmtView(QWidget):
    projects_changed = Signal()               # progetto creato/modificato/Master inizializzato -> ricarica sidebar
    status = Signal(str)

    def __init__(self, api: ApiClient, run: Callable = run_async, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.api, self._run = api, run
        self._users: list[UserOut] = []
        self._projects: list[ProjectOut] = []
        self._perms: list[PermissionOut] = []
        self.history_dialog: Optional[HistoryDialog] = None             # ultimo storico aperto (utile ai test)
        self.branches_dialog: Optional[BranchesDialog] = None           # ultimo elenco branch aperto (utile ai test)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(12)
        lay.addWidget(heading("Utenti & Permessi"))
        lay.addWidget(muted("Il ruolo dell'account è il massimo che l'utente può avere; sul singolo progetto si può "
                            "assegnare lo stesso ruolo o uno inferiore."))
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._build_users_tab()
        self._build_perm_tab()
        self._build_sessions_tab()
        self._build_roles_tab()
        self.tabs.currentChanged.connect(lambda _i: self.reload())

    # ------------------------------------------------------------------ comuni
    def _err(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Errore", exc.detail if isinstance(exc, ApiError) else str(exc))

    def reload(self) -> None:
        i = self.tabs.currentIndex()
        if i == 0:
            self._run(self.api.users, self._fill_users, self._err)
        elif i == 1:
            self._run(lambda: (self.api.users(), self.api.projects()), self._set_perm_base, self._err)
        elif i == 2:
            self._run(lambda: self.api.sessions(all_users=True), self._fill_sessions, self._err)

    # ------------------------------------------------------------------ tab utenti
    def _build_users_tab(self) -> None:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 16, 16, 16)
        bar = QHBoxLayout()
        self.btn_new = QPushButton("  Nuovo utente")
        self.btn_new.setIcon(icon("plus", T["TEXT_ON_ACCENT"], 18))
        self.btn_new.setProperty("variant", "primary")
        self.btn_edit = QPushButton("Modifica...")
        self.btn_toggle = QPushButton("Disattiva")
        self.btn_toggle.setProperty("variant", "danger")
        self.btn_delete_user = QPushButton("Elimina utente...")
        self.btn_delete_user.setProperty("variant", "danger")
        self.btn_delete_user.setToolTip("Elimina definitivamente l'utente (solo Admin). Non puoi eliminare te stesso né l'ultimo amministratore")
        for b in (self.btn_new, self.btn_edit, self.btn_toggle, self.btn_delete_user):
            bar.addWidget(b)
        bar.addStretch(1)
        l.addLayout(bar)
        self.users_table = make_table(["Utente", "Nome", "Email", "Ruolo", "Stato", "Ultimo accesso"], stretch_col=2)
        self.users_table.setColumnWidth(0, 160)
        self.users_table.setColumnWidth(1, 200)
        self.users_table.setColumnWidth(3, 210)
        self.users_table.setColumnWidth(4, 140)
        self.users_table.setColumnWidth(5, 170)
        l.addWidget(self.users_table, 1)
        self.tabs.addTab(w, "Utenti")
        self.btn_new.clicked.connect(self.new_user)
        self.btn_edit.clicked.connect(self.edit_user)
        self.btn_toggle.clicked.connect(self.toggle_user)
        self.btn_delete_user.clicked.connect(lambda _c=False: self.delete_user())
        self.users_table.cellDoubleClicked.connect(lambda *_: self.edit_user())
        self.users_table.itemSelectionChanged.connect(self._sel_changed)

    def _fill_users(self, users: list[UserOut]) -> None:
        self._users = users
        t = self.users_table
        t.setRowCount(len(users))
        for r, u in enumerate(users):
            t.setItem(r, 0, item(u.username, bold=True))
            t.setItem(r, 1, item(u.display_name))
            t.setItem(r, 2, item(u.email, T["MUTED"]))
            t.setItem(r, 3, item(role_label(u.role), role_color(u.role), bold=True))
            state = "Attivo" if u.is_active else "Disattivato"
            if u.is_active and u.must_change_password:
                state = "Cambio password"
            t.setItem(r, 4, item(state, T["GREEN"] if u.is_active else T["RED"]))
            t.setItem(r, 5, item(full_time(u.last_login_at) if u.last_login_at else "mai", T["MUTED"]))
        self._sel_changed()

    def _selected_user(self) -> Optional[UserOut]:
        r = self.users_table.currentRow()
        return self._users[r] if 0 <= r < len(self._users) else None

    def _sel_changed(self) -> None:
        u = self._selected_user()
        self.btn_edit.setEnabled(u is not None)
        self.btn_toggle.setEnabled(u is not None and u.id != self.api.user.id)
        self.btn_delete_user.setEnabled(u is not None and u.id != self.api.user.id)
        self.btn_toggle.setText("Riattiva" if (u and not u.is_active) else "Disattiva")
        self.btn_toggle.setProperty("variant", "success" if (u and not u.is_active) else "danger")
        self.btn_toggle.style().unpolish(self.btn_toggle)
        self.btn_toggle.style().polish(self.btn_toggle)

    def new_user(self) -> None:
        dlg = UserDialog(None, self.api.user.id, self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.create_payload()
        self._run(lambda: self.api.create_user(data), lambda u: (self.status.emit(f"Utente {u.username} creato"),
                                                                  self.reload()), self._err)

    def edit_user(self) -> None:
        u = self._selected_user()
        if u is None:
            return
        dlg = UserDialog(u, self.api.user.id, self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.update_payload()
        self._run(lambda: self.api.update_user(u.id, data),
                  lambda x: (self.status.emit(f"Utente {x.username} aggiornato"), self.reload()), self._err)

    def toggle_user(self) -> None:
        u = self._selected_user()
        if u is None:
            return
        target = not u.is_active
        if not target and QMessageBox.question(
                self, "Disattiva utente",
                f"Disattivare {u.username}? Le sue sessioni verranno revocate subito.") != QMessageBox.Yes:
            return
        self._run(lambda: self.api.update_user(u.id, UserUpdate(is_active=target)),
                  lambda _x: self.reload(), self._err)

    def delete_user(self, confirm: Optional[bool] = None) -> None:
        """Elimina definitivamente l'utente selezionato (solo Admin). ``confirm`` salta la domanda (test)."""
        u = self._selected_user()
        if u is None or u.id == self.api.user.id:
            return
        if confirm is None:
            confirm = QMessageBox.question(
                self, "Elimina utente",
                f"Eliminare definitivamente l'utente «{u.username}»?\n\nLe sue sessioni e i suoi permessi di progetto vengono rimossi. "
                "La cronologia eventi, i commenti e i branch che ha creato restano (con il suo nome). "
                "Per sospendere l'accesso senza cancellare usa «Disattiva».") == QMessageBox.Yes
        if not confirm:
            return
        self._run(lambda: self.api.delete_user(u.id),
                  lambda _x: (self.status.emit(f"Utente {u.username} eliminato"), self.reload()), self._err)

    # ------------------------------------------------------------------ tab permessi
    def _build_perm_tab(self) -> None:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 16, 16, 16)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Progetto"))
        self.proj_combo = QComboBox()
        self.proj_combo.setMinimumWidth(260)
        self.btn_new_proj = QPushButton("  Nuovo progetto...")
        self.btn_new_proj.setIcon(icon("plus", T["TEXT"], 18))
        self.btn_init = QPushButton("  Carica Master iniziale...")
        self.btn_init.setIcon(icon("upload", T["TEXT"], 18))
        self.btn_reimport = QPushButton("  Reimporta file base...")
        self.btn_reimport.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_reimport.setToolTip("Sostituisce il Master di un progetto in lavorazione con un nuovo file base "
                                     "(nuova revisione, cronologia intatta)")
        bar.addWidget(self.proj_combo)
        bar.addWidget(self.btn_new_proj)
        bar.addWidget(self.btn_init)
        bar.addWidget(self.btn_reimport)
        bar.addStretch(1)
        l.addLayout(bar)
        opts = QHBoxLayout()
        self.chk_lead = QCheckBox("Richiedi il Consenso Finale del Lead per i merge dei Developer")
        self.btn_archive = QPushButton("Archivia")
        self.btn_archive.setProperty("variant", "danger")
        self.proj_info = QLabel("")
        self.proj_info.setProperty("muted", True)
        opts.addWidget(self.chk_lead)
        opts.addStretch(1)
        opts.addWidget(self.proj_info)
        opts.addWidget(self.btn_archive)
        l.addLayout(opts)
        l.addWidget(self._build_settings_box())
        l.addWidget(h_line())
        self.perm_table = make_table(["Utente", "Ruolo account", "Ruolo sul progetto", "Effettivo"], stretch_col=0)
        self.perm_table.setColumnWidth(1, 200)
        self.perm_table.setColumnWidth(2, 240)
        self.perm_table.setColumnWidth(3, 200)
        self.perm_table.verticalHeader().setDefaultSectionSize(44)
        l.addWidget(self.perm_table, 1)
        self.tabs.addTab(w, "Permessi di progetto")
        self.proj_combo.activated.connect(lambda _i: self._load_perms())
        self.btn_new_proj.clicked.connect(self.new_project)
        self.btn_init.clicked.connect(self.init_master)
        self.btn_reimport.clicked.connect(self.reimport_master)
        self.chk_lead.clicked.connect(self._toggle_lead)
        self.btn_archive.clicked.connect(self.archive_project)

    def _build_settings_box(self) -> QGroupBox:
        """Gruppo «Impostazioni progetto»: modifica, storico/rollback, export, retention ed eliminazione."""
        box = QGroupBox("Impostazioni progetto")
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 18, 14, 12)
        v.setSpacing(10)
        row1 = QHBoxLayout()
        self.btn_edit_proj = QPushButton("  Modifica progetto...")
        self.btn_edit_proj.setIcon(icon("sliders", T["TEXT"], 18))
        self.btn_history = QPushButton("  Storico e rollback...")
        self.btn_history.setIcon(icon("history", T["TEXT"], 18))
        self.btn_history.setToolTip("Elenco delle revisioni del Master: scarica una revisione o ripristinala come "
                                    "nuova revisione (rollback)")
        self.btn_branches = QPushButton("  Branch...")
        self.btn_branches.setIcon(icon("merge", T["TEXT"], 18))
        self.btn_branches.setToolTip("Branch del progetto: elenco, nuovo, chiusura ed eliminazione "
                                     "(per lavorare su un branch usa il selettore nella barra laterale)")
        self.btn_export = QPushButton("  Esporta progetto (.zip)...")
        self.btn_export.setIcon(icon("download", T["TEXT"], 18))
        self.btn_export.setToolTip("Archivio con le revisioni ancora conservate, lo storico e l'audit del progetto")
        self.btn_delete = QPushButton("  Elimina progetto...")
        self.btn_delete.setIcon(icon("x", T["RED"], 18))
        self.btn_delete.setProperty("variant", "danger")
        self.btn_delete.setToolTip("Elimina definitivamente il progetto, i suoi file sul server e il suo audit")
        for b in (self.btn_edit_proj, self.btn_history, self.btn_branches, self.btn_export):
            row1.addWidget(b)
        row1.addStretch(1)
        row1.addWidget(self.btn_delete)
        v.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Revisioni da conservare sul server"))
        self.spin_revisions = QSpinBox()
        self.spin_revisions.setRange(2, 200)
        self.spin_revisions.setValue(10)
        self.spin_revisions.setMinimumWidth(80)
        self.btn_apply_revisions = QPushButton("Applica")
        self.btn_apply_revisions.setEnabled(False)
        self.lbl_retention = QLabel("")
        self.lbl_retention.setProperty("muted", True)
        row2.addWidget(self.spin_revisions)
        row2.addWidget(self.btn_apply_revisions)
        row2.addSpacing(8)
        row2.addWidget(self.lbl_retention, 1)
        v.addLayout(row2)
        self.btn_edit_proj.clicked.connect(lambda: self.edit_project())
        self.btn_history.clicked.connect(lambda: self.show_history())
        self.btn_branches.clicked.connect(lambda: self.show_branches())
        self.btn_export.clicked.connect(lambda: self.export_project())
        self.btn_delete.clicked.connect(lambda: self.delete_project())
        self.btn_apply_revisions.clicked.connect(lambda: self.apply_retention())
        self.spin_revisions.valueChanged.connect(lambda _v: self._retention_changed())
        return box

    def current_project(self) -> Optional[ProjectOut]:
        pid = self.proj_combo.currentData()
        return next((p for p in self._projects if p.id == pid), None)

    def _set_perm_base(self, res: tuple) -> None:
        users, projects = res
        self._users, self._projects = users, projects
        keep = self.proj_combo.currentData()
        self.proj_combo.blockSignals(True)
        self.proj_combo.clear()
        for p in projects:
            self.proj_combo.addItem(p.name + ("  (archiviato)" if p.archived else ""), p.id)
        i = self.proj_combo.findData(keep)
        self.proj_combo.setCurrentIndex(i if i >= 0 else 0)
        self.proj_combo.blockSignals(False)
        self._load_perms()

    def _load_perms(self) -> None:
        p = self.current_project()
        has = p is not None
        for wdg in (self.chk_lead, self.btn_archive, self.btn_init, self.btn_reimport, self.btn_edit_proj,
                    self.btn_export, self.btn_delete, self.spin_revisions):
            wdg.setEnabled(has)
        self.btn_history.setEnabled(has and p.head_revision is not None)
        self.btn_branches.setEnabled(has and p.head_revision is not None)
        if p is None:
            self.perm_table.setRowCount(0)
            self.proj_info.setText("Nessun progetto: creane uno.")
            self.lbl_retention.setText("")
            self.btn_apply_revisions.setEnabled(False)
            return
        self.spin_revisions.blockSignals(True)
        self.spin_revisions.setValue(max(self.spin_revisions.minimum(), min(self.spin_revisions.maximum(),
                                                                          p.max_revisions)))
        self.spin_revisions.blockSignals(False)
        self._retention_changed()
        self.chk_lead.setChecked(p.require_lead_approval)
        self.btn_archive.setText("Ripristina" if p.archived else "Archivia")
        self.btn_init.setVisible(p.head_revision is None)
        self.btn_reimport.setVisible(p.head_revision is not None)
        self.proj_info.setText(f"Master rev {p.head_revision}" if p.head_revision is not None
                               else "Master non inizializzato")
        self._run(lambda: self.api.permissions(p.id), self._fill_perms, self._err)

    def _fill_perms(self, perms: list[PermissionOut]) -> None:
        self._perms = perms
        t = self.perm_table
        for r in range(t.rowCount()):                       # niente widget residui dalla popolazione precedente
            for c in range(t.columnCount()):
                w = t.cellWidget(r, c)
                if w is not None:
                    w.hide()
                    t.removeCellWidget(r, c)
        t.clearContents()
        t.setRowCount(len(perms))
        for r, pm in enumerate(perms):
            acc = Role.parse(pm.account_role)
            cell = QWidget()
            cell.setObjectName("cell")
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(10, 0, 10, 0)
            cl.setSpacing(10)
            cl.addWidget(Avatar(pm.username, 28))
            nm = QLabel(f"<b>{pm.display_name or pm.username}</b> <span style='color:{T['MUTED']}'>{pm.username}</span>")
            cl.addWidget(nm, 1)
            t.setCellWidget(r, 0, cell)
            t.setItem(r, 1, item(ROLE_LABELS[acc], role_color(acc.key), bold=True))
            if acc == Role.ADMIN:
                t.setItem(r, 2, item("Accesso globale", T["MUTED"]))
                t.setItem(r, 3, item(ROLE_LABELS[Role.ADMIN], T["RED"]))
                continue
            cb = _role_combo(max_role=acc, allow_none=True, include_admin=False)
            i = cb.findData(pm.role)
            cb.setCurrentIndex(max(0, i))
            cb.activated.connect(lambda _i, c=cb, u=pm.user_id: self._perm_changed(u, c.currentData()))
            holder = QWidget()
            holder.setObjectName("cell")
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 4, 6, 4)
            hl.addWidget(cb)
            t.setCellWidget(r, 2, holder)
            eff = pm.effective_role
            t.setItem(r, 3, item(role_label(eff) if eff else "nessun accesso",
                                 role_color(eff) if eff else T["MUTED"]))

    def _perm_changed(self, user_id: int, role: Optional[str]) -> None:
        p = self.current_project()
        if p is None:
            return
        self._run(lambda: self.api.set_permission(p.id, user_id, role),
                  lambda perms: (self._fill_perms(perms), self.status.emit("Permessi aggiornati"),
                                 self.projects_changed.emit()), lambda e: (self._err(e), self._load_perms()))

    def new_project(self) -> None:
        dlg = NewProjectDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.payload()

        def ok(p: ProjectOut) -> None:
            self.status.emit(f"Progetto «{p.name}» creato")
            self.projects_changed.emit()
            self.reload()
        self._run(lambda: self.api.create_project(data), ok, self._err)

    def init_master(self, path: Optional[str] = None) -> None:
        p = self.current_project()
        if p is None:
            return
        if not isinstance(path, str):
            path, _ = QFileDialog.getOpenFileName(self, "Master iniziale (qualsiasi file; Rockwell: .L5K / .L5X)", "",
                                                  import_filters())
        if not path:
            return

        def ok(_m) -> None:
            self.status.emit(f"Master iniziale caricato in «{p.name}»")
            self.projects_changed.emit()
            self.reload()
        self._run(lambda: self.api.init_master_for(p.id, Path(path)), ok, self._err)

    def reimport_master(self, path: Optional[str] = None, reason: Optional[str] = None) -> None:
        """Progetto in lavorazione: nuova revisione del Master da un file base aggiornato (storico intatto).

        ``path`` e ``reason`` servono ai test (e a chi vuole saltare le domande): se mancano vengono chiesti.
        """
        p = self.current_project()
        if p is None or p.head_revision is None:
            return
        ask = not isinstance(path, str)
        if ask:
            path, _ = QFileDialog.getOpenFileName(self, f"Nuovo file base di «{p.name}»", "", import_filters())
        if not path:
            return
        if ask and QMessageBox.question(
                self, "Reimporta file base",
                f"Dal file «{Path(path).name}» verrà creata una NUOVA revisione del Master di «{p.name}».\n\n"
                "• la cronologia resta intatta (si può sempre tornare alle revisioni precedenti);\n"
                "• le richieste di merge in corso vengono rianalizzate sul nuovo Master e i consensi già dati "
                "vengono azzerati;\n• i client vengono avvisati del nuovo Master.\n\nProcedere?") != QMessageBox.Yes:
            return
        if not isinstance(reason, str):
            reason, ok_ = QInputDialog.getMultiLineText(
                self, "Reimporta file base", "Motivo della reimportazione (minimo 5 caratteri):")
            if not ok_:
                return
        reason = reason.strip()
        if len(reason) < 5:
            QMessageBox.warning(self, "Motivo troppo breve", "Indica un motivo di almeno 5 caratteri.")
            return

        def ok(m) -> None:
            self.status.emit(f"File base reimportato in «{p.name}»: nuova rev {m.revision}")
            self.projects_changed.emit()
            self.reload()
        self._run(lambda: self.api.reimport_master_for(p.id, Path(path), reason), ok, self._err)

    # ------------------------------------------------------------------ impostazioni progetto (solo Admin)
    @staticmethod
    def kept_revisions(p: ProjectOut) -> int:
        """Revisioni del Master oggi conservate sul server (le altre sono state eliminate dal limite)."""
        return max(0, (p.head_revision or 0) - (p.purged_revisions or 0))

    def _retention_changed(self) -> None:
        p = self.current_project()
        if p is None:
            return
        self.btn_apply_revisions.setEnabled(self.spin_revisions.value() != p.max_revisions)
        total = p.head_revision or 0
        self.lbl_retention.setText(f"Conservate: {self.kept_revisions(p)} di {total} · "
                                   f"limite attuale: {p.max_revisions}")

    def edit_project(self, name: Optional[str] = None, description: Optional[str] = None) -> None:
        """Modifica nome e/o descrizione. ``name``/``description`` (test) evitano la finestra di dialogo."""
        p = self.current_project()
        if p is None:
            return
        if name is None and description is None:
            dlg = EditProjectDialog(p, self)
            if dlg.exec() != QDialog.Accepted:
                return
            data = dlg.payload()
        else:
            name = p.name if name is None else name.strip()
            desc = (p.description or "") if description is None else description.strip()
            if not name:
                QMessageBox.warning(self, "Nome mancante", "Il progetto deve avere un nome.")
                return
            data = ProjectUpdate(name=name if name != p.name else None,
                                 description=desc if desc != (p.description or "") else None)
        if data.name is None and data.description is None:
            return                                                       # niente da salvare

        def ok(x: ProjectOut) -> None:
            self.status.emit(f"Progetto «{x.name}» aggiornato")
            self.projects_changed.emit()
            self.reload()
        self._run(lambda: self.api.update_project(p.id, data), ok, self._err)

    def apply_retention(self, value: Optional[int] = None, confirm: Optional[bool] = None) -> None:
        """Imposta il numero di revisioni conservate dal server. Se e' inferiore a quelle oggi conservate chiede
        conferma (``confirm`` e' per i test: True/False risponde al posto dell'utente)."""
        p = self.current_project()
        if p is None:
            return
        value = int(self.spin_revisions.value() if value is None else value)
        if value == p.max_revisions:
            return
        kept = self.kept_revisions(p)
        if value < kept:
            n = kept - value
            ok_ = confirm if isinstance(confirm, bool) else QMessageBox.question(
                self, "Riduci le revisioni conservate",
                f"Il limite passa da {p.max_revisions} a {value} revisioni.\n\n"
                f"Verranno eliminate definitivamente dal server le {n} revisioni più vecchie "
                f"(oggi ne sono conservate {kept}). Nello storico resteranno i dati (autore, data, messaggio) "
                "ma quelle revisioni non si potranno più scaricare né ripristinare.\n\nProcedere?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
            if not ok_:                                                  # annullato: la vista torna al limite reale
                self.spin_revisions.blockSignals(True)
                self.spin_revisions.setValue(max(self.spin_revisions.minimum(), min(self.spin_revisions.maximum(),
                                                                                    p.max_revisions)))
                self.spin_revisions.blockSignals(False)
                self._retention_changed()
                return

        def done(x: ProjectOut) -> None:
            self.status.emit(f"«{x.name}»: il server conserverà le ultime {x.max_revisions} revisioni")
            self.projects_changed.emit()
            self.reload()
        self._run(lambda: self.api.update_project(p.id, ProjectUpdate(max_revisions=value)), done, self._err)

    # --- branch
    def show_branches(self) -> None:
        """Elenco dei branch del progetto selezionato (console Admin: elenco, nuovo, chiudi, elimina; il cambio di
        contesto si fa dal selettore della barra laterale)."""
        p = self.current_project()
        if p is None or p.head_revision is None:
            return
        dlg = BranchesDialog(self.api, p, self.api.role, None, self._run, self, manage_only=True)
        self.branches_dialog = dlg
        dlg.exec()

    # --- storico e rollback
    def show_history(self) -> None:
        """Apre lo storico del progetto selezionato (senza cambiare il progetto aperto nell'api condivisa)."""
        p = self.current_project()
        if p is None or p.head_revision is None:
            return
        api = self.api
        self._run(lambda: api.history_for(p.id), lambda h: self._show_history(p, h), self._err)

    def _show_history(self, p: ProjectOut, history: list) -> None:
        dlg = HistoryDialog(history, Role.ADMIN, self, max_revisions=p.max_revisions)
        self.history_dialog = dlg
        dlg.download_requested.connect(lambda rev: self.download_revision(rev, project=p))
        dlg.restore_requested.connect(lambda rev: self.rollback(rev, project=p, dialog=dlg))
        HistoryActions(dlg, self.api, self._run, pid=p.id, on_status=self.status.emit, on_error=self._err)    # confronta / etichetta / blocca
        dlg.exec()

    def download_revision(self, rev: int, path: Optional[str] = None, project: Optional[ProjectOut] = None) -> None:
        """Salva una revisione del Master (nome proposto ``<nome>_revN<ext>``, come il Checkout del client)."""
        p = project or self.current_project()
        if p is None:
            return
        if not isinstance(path, str):
            path, _ = QFileDialog.getSaveFileName(self, "Salva revisione",
                                                  revision_save_name(p.master_name, rev, p.master_ext),
                                                  revision_save_filter(p.master_ext))
        if not path:
            return
        api = self.api
        self._run(lambda: api.download_master_for(p.id, Path(path), rev),
                  lambda _r: self.status.emit(f"«{p.name}»: rev {rev} salvata in {path}"), self._err)

    def rollback(self, rev: int, reason: Optional[str] = None, project: Optional[ProjectOut] = None,
                 dialog: Optional[HistoryDialog] = None) -> None:
        """Rollback: ripristina la revisione ``rev`` come NUOVA revisione del Master (cronologia intatta).

        ``reason`` (test) salta le domande; altrimenti chiede conferma e motivo (almeno 5 caratteri).
        """
        p = project or self.current_project()
        if p is None:
            return
        if not isinstance(reason, str):
            if QMessageBox.question(
                    self, "Ripristina revisione",
                    f"Verrà creata una NUOVA revisione del Master di «{p.name}» con il contenuto della rev {rev}.\n\n"
                    "• la cronologia resta intatta: nessuna revisione viene cancellata;\n"
                    "• i client vengono avvisati del nuovo Master;\n"
                    "• l'operazione è registrata nell'audit.\n\nProcedere?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            reason, ok_ = QInputDialog.getMultiLineText(
                self, "Ripristina revisione", f"Motivo del ripristino della rev {rev} (minimo {MIN_REASON} caratteri):")
            if not ok_:
                return
        reason = reason.strip()
        if len(reason) < MIN_REASON:
            QMessageBox.warning(self, "Motivo troppo breve", f"Indica un motivo di almeno {MIN_REASON} caratteri.")
            return
        api = self.api

        def done(m) -> None:
            self.status.emit(f"«{p.name}» ripristinato dalla rev {rev}: nuova rev {m.revision}")
            self.projects_changed.emit()
            self.reload()
            if dialog is not None:                                        # lo storico aperto mostra la nuova revisione
                self._run(lambda: api.history_for(p.id), lambda h: dialog.set_history(h), self._err)
        self._run(lambda: api.restore_for(p.id, rev, reason), done, self._err)

    # --- export / eliminazione
    @staticmethod
    def _export_name(p: ProjectOut) -> str:
        stem = re.sub(r"[^\w.-]+", "_", p.name).strip("_") or "progetto"
        return f"{stem}_export_{datetime.now():%Y%m%d}.zip"

    def export_project(self, path: Optional[str] = None) -> None:
        """Esporta un archivio .zip del progetto (revisioni conservate, storico, audit CSV)."""
        p = self.current_project()
        if p is None:
            return
        if not isinstance(path, str):
            path, _ = QFileDialog.getSaveFileName(self, "Esporta progetto", self._export_name(p),
                                                  "Archivio zip (*.zip);;Tutti i file (*)")
        if not path:
            return
        api = self.api
        self._run(lambda: api.export_project(p.id, Path(path)),
                  lambda _r: self.status.emit(f"Progetto «{p.name}» esportato in {path}"), self._err)

    def delete_project(self, confirm_name: Optional[str] = None, export_to: Optional[str] = None) -> None:
        """Elimina DEFINITIVAMENTE il progetto selezionato (file, revisioni, permessi, audit).

        Senza argomenti mostra la conferma forte (nome da digitare + esportazione preventiva). ``confirm_name`` e
        ``export_to`` (test) sostituiscono dialogo e scelta del file: ``export_to`` e' il .zip da creare PRIMA
        dell'eliminazione (se l'export fallisce non viene eliminato nulla).
        """
        p = self.current_project()
        if p is None:
            return
        if confirm_name is None:
            dlg = DeleteProjectDialog(p, self)
            if dlg.exec() != QDialog.Accepted:
                return
            confirm_name = dlg.typed_name()
            if dlg.wants_export():
                export_to, _ = QFileDialog.getSaveFileName(self, "Salva l'archivio prima di eliminare",
                                                           self._export_name(p), "Archivio zip (*.zip);;Tutti i file (*)")
                if not export_to:
                    return                                                # niente archivio -> niente eliminazione
        if confirm_name != p.name:
            QMessageBox.warning(self, "Nome non corrispondente",
                                "Il nome digitato non coincide con quello del progetto: nulla è stato eliminato.")
            return
        api, name, dest = self.api, p.name, (Path(export_to) if export_to else None)

        def work():
            if dest is not None:
                api.export_project(p.id, dest)                            # se fallisce, non si elimina
            return api.delete_project(p.id, confirm_name)

        def ok(res) -> None:
            extra = f" (archivio salvato in {dest})" if dest is not None else ""
            self.status.emit(f"Progetto «{name}» eliminato: {res.audit_entries_removed} righe di audit rimosse{extra}")
            self.projects_changed.emit()
            self.reload()
        self._run(work, ok, self._err)

    def _toggle_lead(self) -> None:
        p = self.current_project()
        if p is None:
            return
        val = self.chk_lead.isChecked()
        self._run(lambda: self.api.update_project(p.id, ProjectUpdate(require_lead_approval=val)),
                  lambda _p: (self.projects_changed.emit(), self.reload()), self._err)

    def archive_project(self) -> None:
        p = self.current_project()
        if p is None:
            return
        target = not p.archived
        if target and QMessageBox.question(self, "Archivia progetto",
                                           f"Archiviare «{p.name}»? Non accetterà nuove richieste di merge.") != QMessageBox.Yes:
            return
        self._run(lambda: self.api.update_project(p.id, ProjectUpdate(archived=target)),
                  lambda _p: (self.projects_changed.emit(), self.reload()), self._err)

    # ------------------------------------------------------------------ tab sessioni
    def _build_sessions_tab(self) -> None:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 16, 16, 16)
        bar = QHBoxLayout()
        self.btn_revoke = QPushButton("Revoca sessione")
        self.btn_revoke.setProperty("variant", "danger")
        bar.addWidget(self.btn_revoke)
        bar.addStretch(1)
        l.addLayout(bar)
        self.sess_table = make_table(["Utente", "Client", "IP", "Iniziata", "Ultima attività", "Scade", "Stato"],
                                     stretch_col=1)
        self.sess_table.setColumnWidth(0, 140)
        self.sess_table.setColumnWidth(2, 120)
        for c in (3, 4, 5):
            self.sess_table.setColumnWidth(c, 150)
        l.addWidget(self.sess_table, 1)
        self.tabs.addTab(w, "Sessioni")
        self._sessions = []
        self.btn_revoke.clicked.connect(self.revoke_session)

    def _fill_sessions(self, sessions: list) -> None:
        self._sessions = sessions
        t = self.sess_table
        t.setRowCount(len(sessions))
        for r, s in enumerate(sessions):
            state, col = ("Revocata", T["RED"]) if s.revoked else (("Questa sessione", T["ACCENT"]) if s.current
                                                                     else ("Attiva", T["GREEN"]))
            vals = [(s.username, None), (s.client, T["MUTED"]), (s.ip, T["MUTED"]), (full_time(s.created_at), None),
                    (full_time(s.last_seen), None), (full_time(s.expires_at), T["MUTED"]), (state, col)]
            for c, (v, color) in enumerate(vals):
                t.setItem(r, c, item(v, color, bold=(c == 0)))

    def revoke_session(self) -> None:
        r = self.sess_table.currentRow()
        if not (0 <= r < len(self._sessions)):
            return
        s = self._sessions[r]
        if s.revoked:
            return
        if QMessageBox.question(self, "Revoca sessione",
                                f"Revocare la sessione di {s.username}? Dovrà effettuare di nuovo il login.") != QMessageBox.Yes:
            return
        self._run(lambda: self.api.revoke_session(s.id), lambda _x: self.reload(), self._err)

    # ------------------------------------------------------------------ tab ruoli
    def _build_roles_tab(self) -> None:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 16, 16, 16)
        l.setSpacing(12)
        for r in sorted(Role, reverse=True):
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 12, 16, 12)
            top = QHBoxLayout()
            name = QLabel(ROLE_LABELS[r])
            name.setStyleSheet(f"font-weight:700; font-size:14px; color:{role_color(r.key)};")
            top.addWidget(name)
            top.addWidget(chip(r.key, T["MUTED"]))
            top.addStretch(1)
            cl.addLayout(top)
            cl.addWidget(muted(ROLE_DESCRIPTIONS[r]))
            l.addWidget(card)
        l.addStretch(1)
        self.tabs.addTab(w, "Ruoli")
