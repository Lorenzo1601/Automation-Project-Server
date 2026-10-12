"""Gestione utenti, permessi di progetto, sessioni e ruoli (solo Admin)."""
from __future__ import annotations

import secrets
import string
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from client.api_client import ApiClient, ApiError
from client.theme.tokens import T
from client.theme.widgets import Avatar, icon, role_color
from client.views.project_dialogs import NewProjectDialog                  # noqa: F401 - riesportato
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
        for b in (self.btn_new, self.btn_edit, self.btn_toggle):
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
        for wdg in (self.chk_lead, self.btn_archive, self.btn_init, self.btn_reimport):
            wdg.setEnabled(has)
        if p is None:
            self.perm_table.setRowCount(0)
            self.proj_info.setText("Nessun progetto: creane uno.")
            return
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
