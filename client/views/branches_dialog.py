"""Elenco e gestione dei branch del progetto aperto: nuovo, passa, aggiorna dal principale, unisci nel principale,
chiudi, elimina. Il Viewer vede soltanto.

Il dialogo esegue da solo creazione / chiusura / eliminazione; per «Passa», «Aggiorna dal principale» e «Unisci nel
principale» emette un segnale e si chiude: li orchestra la finestra principale (cambio di contesto, apertura della
richiesta di merge risultante)."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from client.api_client import ApiClient, ApiError
from client.branch_info import (abilities, can_create, kind_label, my_open_personal, state_label, summary_text,
                                sync_text)
from client.theme.tokens import T
from client.theme.widgets import icon
from client.views.common import heading, item, make_table, muted
from client.workers import run_async
from common.roles import Role
from common.schemas import ProjectOut
from common.schemas_branches import MAX_PERSONAL_BRANCHES, BranchOut

KIND_COLORS = {"personal": T["ACCENT_ALT"], "team": T["SYN_KEYWORD"]}
STATE_COLORS = {"open": T["GREEN"], "merged": T["ACCENT"], "closed": T["MUTED"]}
COLS = ["Nome", "Tipo", "Proprietario", "Stato", "Base (principale)", "Testa", "Rispetto al principale"]


# ======================================================================================= nuovo branch
class NewBranchDialog(QDialog):
    """Nome, tipo (personale / team) e punto di partenza (testa o una revisione del principale)."""

    def __init__(self, role: Role, revisions: list[int], personal_open: int = 0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.role = role
        self._personal_open = personal_open
        self.setWindowTitle("Nuovo branch")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(heading("Nuovo branch", 2))
        lay.addWidget(muted("Un branch è una linea alternativa del principale, con le sue revisioni."))
        form = QFormLayout()
        form.setSpacing(10)
        self.name = QLineEdit()
        self.name.setMaxLength(60)
        self.name.setPlaceholderText("es. nuova-logica-nastro")
        form.addRow("Nome", self.name)
        self.kind = QComboBox()
        self.kind.addItem("Personale — scrivi solo tu, con salvataggi diretti", "personal")
        self.kind.addItem("Team — si lavora con richieste di merge e consenso (Lead/Admin)", "team")
        ok_team, why_team = can_create(role, "team")
        if not ok_team:
            m = self.kind.model().item(1)
            m.setEnabled(False)
            m.setToolTip(why_team)
        form.addRow("Tipo", self.kind)
        lay.addLayout(form)

        self.rb_head = QRadioButton("Dalla testa del principale")
        self.rb_head.setChecked(True)
        self.rb_rev = QRadioButton("Da una revisione del principale:")
        self.rev = QComboBox()
        for r in sorted(revisions, reverse=True):
            self.rev.addItem(f"rev {r}", r)
        self.rev.setEnabled(False)
        self.rb_rev.setEnabled(bool(revisions))
        row = QHBoxLayout()
        row.addWidget(self.rb_rev)
        row.addWidget(self.rev, 1)
        lay.addWidget(self.rb_head)
        lay.addLayout(row)
        self.rb_rev.toggled.connect(self.rev.setEnabled)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{T['RED']};")
        lay.addWidget(self.error)
        self.bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Save).setText("Crea")
        self.bb.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        self.bb.button(QDialogButtonBox.Cancel).setText("Annulla")
        self.bb.accepted.connect(self._accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

    def _accept(self) -> None:
        if not self.name.text().strip():
            self.error.setText("Indica il nome del branch.")
            return
        ok, why = can_create(self.role, self.kind.currentData(), self._personal_open)
        if not ok:
            self.error.setText(why)
            return
        self.accept()

    def payload(self) -> tuple[str, str, Optional[int]]:
        """(nome, tipo, revisione di partenza del principale o None = testa)."""
        rev = self.rev.currentData() if (self.rb_rev.isChecked() and self.rev.count()) else None
        return self.name.text().strip(), self.kind.currentData(), rev


# ======================================================================================= elenco
class BranchesDialog(QDialog):
    created_branch = Signal(str)         # slug del branch appena creato (la finestra principale ci passa e ne scarica la copia)
    switch_requested = Signal(str)       # slug del branch a cui passare ("" = principale)
    sync_requested = Signal(str)         # «Aggiorna dal principale» su questo branch
    merge_requested = Signal(str)        # «Unisci nel principale» per questo branch
    preview_requested = Signal(str, str)  # (slug, 'merge'|'sync'): anteprima dei conflitti, senza effetti
    changed = Signal()                   # creato / chiuso / eliminato: la finestra principale ricarica l'elenco

    def __init__(self, api: ApiClient, project: ProjectOut, role: Role, current: Optional[str] = None,
                 run: Callable = run_async, parent: Optional[QWidget] = None, manage_only: bool = False):
        super().__init__(parent)
        self.api, self.project, self.role, self.current, self._run = api, project, role, current, run
        self.manage_only = manage_only          # console Admin: solo elenco / nuovo / chiudi / elimina (niente cambio di contesto)
        self._rows: list[BranchOut] = []
        # hook sostituibili nei test (nessun dialogo modale)
        self.confirm: Callable[[str, str], bool] = self._confirm
        self.ask_new: Callable[[list[int], int], Optional[tuple]] = self._ask_new
        self.setWindowTitle(f"Branch · {project.name}")
        self.resize(1080, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        lay.addWidget(heading("Branch del progetto", 2))
        self.lbl_info = muted("")
        lay.addWidget(self.lbl_info)
        self.table = make_table(COLS, stretch_col=0)
        for c, w in ((1, 100), (2, 130), (3, 90), (4, 130), (5, 80), (6, 190)):
            self.table.setColumnWidth(c, w)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        lay.addWidget(self.table, 1)
        self.lbl_sel = muted("")
        lay.addWidget(self.lbl_sel)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_new = QPushButton("  Nuovo branch...")
        self.btn_new.setIcon(icon("plus", T["TEXT_ON_ACCENT"], 18))
        self.btn_new.setProperty("variant", "primary")
        self.btn_switch = QPushButton("Passa a questo branch")
        self.btn_main = QPushButton("Passa al principale")
        self.btn_sync = QPushButton("Aggiorna dal principale")
        self.btn_merge = QPushButton("Unisci nel principale...")
        self.btn_preview = QPushButton("Anteprima conflitti")
        self.btn_preview.setToolTip("Simula l'unione nel principale (o l'aggiornamento dal principale) e mostra i conflitti, "
                                    "senza creare richieste")
        self.btn_close_branch = QPushButton("Chiudi")
        self.btn_delete = QPushButton("Elimina...")
        self.btn_delete.setProperty("variant", "danger")
        self.btn_reload = QPushButton()
        self.btn_reload.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_reload.setToolTip("Ricarica l'elenco")
        self.btn_reload.setProperty("variant", "ghost")
        for b in (self.btn_new, self.btn_switch, self.btn_main, self.btn_sync, self.btn_merge, self.btn_preview,
                  self.btn_close_branch, self.btn_delete, self.btn_reload):
            row.addWidget(b)
        row.addStretch(1)
        self.btn_done = QPushButton("Chiudi finestra")
        row.addWidget(self.btn_done)
        lay.addLayout(row)

        if manage_only:
            for w in (self.btn_switch, self.btn_main, self.btn_sync, self.btn_merge, self.btn_preview):
                w.setVisible(False)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self.btn_switch.isEnabled() and self.switch_to_selected())
        self.btn_new.clicked.connect(lambda: self.new_branch())
        self.btn_switch.clicked.connect(lambda: self.switch_to_selected())
        self.btn_main.clicked.connect(lambda: (self.accept(), self.switch_requested.emit("")))
        self.btn_sync.clicked.connect(lambda: self.sync_selected())
        self.btn_merge.clicked.connect(lambda: self.merge_selected())
        self.btn_preview.clicked.connect(lambda _c=False: self.preview_selected())
        self.btn_close_branch.clicked.connect(lambda: self.close_selected())
        self.btn_delete.clicked.connect(lambda: self.delete_selected())
        self.btn_reload.clicked.connect(lambda: self.reload())
        self.btn_done.clicked.connect(self.accept)
        self._update_buttons()
        self.reload()

    # ------------------------------------------------------------------ dati
    def _err(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Errore", exc.detail if isinstance(exc, ApiError) else str(exc))

    def reload(self) -> None:
        pid = self.project.id
        self._run(lambda: self.api.branches(pid), self.set_branches, self._err)

    def set_branches(self, branches: list[BranchOut], keep: Optional[str] = None) -> None:
        try:
            keep = keep or (self.selected().slug if self.selected() else None)
        except RuntimeError:                                             # dialogo gia' chiuso
            return
        self._rows = sorted(branches, key=lambda b: (b.state != "open", b.kind != "team", b.name.lower()))
        t = self.table
        t.setRowCount(len(self._rows))
        for r, b in enumerate(self._rows):
            mark = "● " if b.slug == self.current else ""
            dim = T["MUTED"] if b.state != "open" else None
            t.setItem(r, 0, item(mark + b.name, dim, data=b.slug, bold=b.slug == self.current))
            t.setItem(r, 1, item(kind_label(b.kind).capitalize(), KIND_COLORS.get(b.kind), bold=True))
            t.setItem(r, 2, item(b.owner, dim))
            t.setItem(r, 3, item(state_label(b.state).capitalize(), STATE_COLORS.get(b.state)))
            t.setItem(r, 4, item(f"rev {b.base_main_revision}", dim))
            t.setItem(r, 5, item(f"rev {b.head_revision}", dim))
            t.setItem(r, 6, item(sync_text(b), T["ORANGE"] if (b.behind and b.state == "open") else dim))
        n_mine = my_open_personal(self._rows, self.api.user.username if self.api.user else "")
        self.lbl_info.setText(f"{sum(1 for b in self._rows if b.state == 'open')} branch aperti · "
                              f"tuoi personali aperti: {n_mine}/{MAX_PERSONAL_BRANCHES}"
                              + (f" · branch corrente: {self.current}" if self.current else " · stai sul principale"))
        sel = next((i for i, b in enumerate(self._rows) if b.slug == keep), -1)
        if sel < 0 and self.current:
            sel = next((i for i, b in enumerate(self._rows) if b.slug == self.current), -1)
        if sel >= 0:
            t.selectRow(sel)
        self._update_buttons()

    def selected(self) -> Optional[BranchOut]:
        r = self.table.currentRow()
        return self._rows[r] if 0 <= r < len(self._rows) and self.table.selectedItems() else None

    # ------------------------------------------------------------------ abilitazioni
    def _update_buttons(self) -> None:
        b = self.selected()
        viewer = self.role < Role.DEVELOPER
        ok_p, why_p = can_create(self.role, "personal", my_open_personal(
            self._rows, self.api.user.username if self.api.user else ""))
        ok_new = ok_p or can_create(self.role, "team")[0]
        self.btn_new.setEnabled(ok_new)
        self.btn_new.setToolTip("" if ok_new else why_p)
        self.btn_main.setEnabled(bool(self.current))
        if b is None:
            for w in (self.btn_switch, self.btn_sync, self.btn_merge, self.btn_preview, self.btn_close_branch, self.btn_delete):
                w.setEnabled(False)
            self.lbl_sel.setText("Seleziona un branch.")
            return
        ab = abilities(b, self.role, self.current)
        self.btn_switch.setEnabled(ab.switch)
        self.btn_sync.setEnabled(ab.sync)
        self.btn_merge.setEnabled(ab.merge)
        self.btn_preview.setEnabled(ab.merge or ab.sync)
        self.btn_close_branch.setEnabled(ab.close)
        self.btn_delete.setEnabled(ab.delete)
        self.btn_sync.setToolTip("Porta nel branch le novità del principale" if ab.sync else
                                 "Già aggiornato rispetto al principale" if ab.merge else "Non disponibile")
        self.btn_close_branch.setToolTip("" if ab.close else "Solo il proprietario (o un Admin) chiude un branch aperto")
        self.btn_delete.setToolTip("" if ab.delete else "Solo il proprietario (o un Admin) elimina un branch")
        extra = ""
        if viewer:
            extra = " · sola lettura (Viewer)"
        elif b.state != "open":
            extra = f" · {state_label(b.state)}: sola lettura"
        elif not b.can_write:
            extra = f" · sola lettura per te (branch di {b.owner})"
        self.lbl_sel.setText(f"«{b.name}»: {summary_text(b)} · testa rev {b.head_revision}{extra}")

    # ------------------------------------------------------------------ azioni
    def switch_to_selected(self) -> None:
        b = self.selected()
        if b is None or not abilities(b, self.role, self.current).switch:
            return
        self.accept()
        self.switch_requested.emit(b.slug)

    def sync_selected(self) -> None:
        b = self.selected()
        if b is None or not abilities(b, self.role, self.current).sync:
            return
        self.accept()
        self.sync_requested.emit(b.slug)

    def merge_selected(self) -> None:
        b = self.selected()
        if b is None or not abilities(b, self.role, self.current).merge:
            return
        self.accept()
        self.merge_requested.emit(b.slug)

    def preview_selected(self, mode: Optional[str] = None) -> None:
        """«Anteprima conflitti»: ``mode`` None = chiede se simulare l'unione nel principale o l'aggiornamento dal principale."""
        b = self.selected()
        if b is None:
            return
        ab = abilities(b, self.role, self.current)
        if mode is None:
            mode = "merge" if ab.merge and not ab.sync else "sync" if ab.sync and not ab.merge else self.ask_mode()
        if mode not in ("merge", "sync") or not (ab.merge if mode == "merge" else ab.sync):
            return
        self.accept()
        self.preview_requested.emit(b.slug, mode)

    def ask_mode(self) -> Optional[str]:
        box = QMessageBox(self)
        box.setWindowTitle("Anteprima conflitti")
        box.setText("Quale operazione vuoi simulare?")
        bm = box.addButton("Unione nel principale", QMessageBox.AcceptRole)
        bs = box.addButton("Aggiornamento dal principale", QMessageBox.AcceptRole)
        box.addButton("Annulla", QMessageBox.RejectRole)
        box.exec()
        return "merge" if box.clickedButton() is bm else "sync" if box.clickedButton() is bs else None

    def close_selected(self) -> None:
        b = self.selected()
        if b is None or not abilities(b, self.role, self.current).close:
            return
        if not self.confirm("Chiudi branch", f"Chiudere il branch «{b.name}»?\nResterà consultabile ma di sola lettura: "
                                             "non si potrà più scrivere né unire nel principale."):
            return
        api, slug, pid = self.api, b.slug, self.project.id
        self._run(lambda: api.close_branch(slug, pid), lambda _x: (self.changed.emit(), self.reload()), self._err)

    def delete_selected(self) -> None:
        b = self.selected()
        if b is None or not abilities(b, self.role, self.current).delete:
            return
        if not self.confirm("Elimina branch", f"Eliminare definitivamente il branch «{b.name}» e le sue revisioni?\n"
                                              "L'operazione non si può annullare (i file già scaricati sul tuo PC "
                                              "restano dove sono)."):
            return
        api, slug, pid = self.api, b.slug, self.project.id
        self._run(lambda: api.delete_branch(slug, pid), lambda _x: (self.changed.emit(), self.reload()), self._err)

    def new_branch(self) -> None:
        """Nuovo branch: chiede nome/tipo/punto di partenza e lo crea sul server (poi propone di passarci)."""
        proj, api = self.project, self.api
        mine = my_open_personal(self._rows, api.user.username if api.user else "")

        def with_history(history) -> None:
            revs = [m.revision for m in history if not m.purged]
            self._create_with(revs, mine)

        def no_history(_exc: Exception) -> None:
            n = proj.head_revision or 0
            self._create_with(list(range(1, n + 1)), mine)
        self._run(lambda: api.history_for(proj.id), with_history, no_history)         # sempre le revisioni del principale

    def _create_with(self, revisions: list[int], personal_open: int) -> None:
        res = self.ask_new(revisions, personal_open)
        if res is None:
            return
        name, kind, rev = res
        api, pid = self.api, self.project.id

        def done(b: BranchOut) -> None:
            self.changed.emit()
            self.created = b
            self.reload()
            self.created_branch.emit(b.slug)
        self._run(lambda: api.create_branch(name, kind, rev, pid), done, self._err)

    # ------------------------------------------------------------------ dialoghi (sostituibili)
    def _ask_new(self, revisions: list[int], personal_open: int) -> Optional[tuple]:
        dlg = NewBranchDialog(self.role, revisions, personal_open, self)
        return dlg.payload() if dlg.exec() == QDialog.Accepted else None

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    created: Optional[BranchOut] = None          # ultimo branch creato da questo dialogo (utile ai test)
