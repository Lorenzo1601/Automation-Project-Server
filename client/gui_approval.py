"""Schermata di Consenso e Diff (tema scuro).

Per una richiesta di merge mostra:
  * le modifiche applicabili senza conflitto (dalla proposta e gia' presenti nel Master) con diff Ladder,
  * i conflitti con vista a tre pannelli (Base | Proposta | Master) e scelta di risoluzione,
  * i partecipanti richiesti, il loro consenso e lo stato del Consenso Finale del Lead,
  * i pulsanti "Dai Consenso al Merge" / "Rifiuta / Chiedi Modifiche" e, per l'Admin, "Forza Merge".

Cosa puo' fare l'utente e' deciso da ``compute_permissions`` (funzione pura, testata): il server applica
comunque le stesse regole, la GUI si limita ad abilitare i pulsanti giusti.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from client.api_client import ApiClient, ApiError
from client.theme.tokens import T
from client.theme.widgets import Avatar, StatusBadge, badge_kind
from client.views.common import chip, drop_widget, h_line, human_time, polish, role_chip
from client.views.diff_viewer import DiffViewer, ThreeWayView
from client.workers import run_async
from common.roles import Role
from common.schemas import ChangeOut, ConflictOut, ProposalDetail, ProposalState, ResolutionIn

KIND_IT = {"added": "Aggiunto", "modified": "Modificato", "deleted": "Eliminato"}
KIND_COLOR = {"added": T["GREEN"], "modified": T["ORANGE"], "deleted": T["RED"]}
ORIGIN_IT = {"proposal": "Proposta", "master": "Master", "both": "Entrambi"}
CHOICE_IT = {"mine": "Mia proposta", "theirs": "Master", "both": "Entrambe", "custom": "Manuale"}
DECISION_RING = {"approve": T["GREEN"], "reject": T["RED"], "pending": T["ORANGE"]}
DECISION_TEXT = {"approve": "consenso dato", "reject": "rifiutato", "pending": "in attesa"}
OPEN_STATES = (ProposalState.AWAITING_CONSENT, ProposalState.CONFLICT)


# ======================================================================================= permessi (pura)
@dataclass(frozen=True)
class Permissions:
    approve: bool
    reject: bool
    cancel: bool
    resolve: bool          # puo' scegliere le risoluzioni dei conflitti
    force: bool
    read_only: bool
    approve_as_lead: bool  # il consenso che darebbe e' il Consenso Finale del Lead


def compute_permissions(d: ProposalDetail, me: str, role: Role, pending_resolutions: int = 0) -> Permissions:
    """Regole del consenso (specchio di ``SessionManager``): vedi README, sezione Consenso."""
    open_ = d.state in OPEN_STATES
    am_author = d.author_session == me
    mine = next((p for p in d.participants if p.session_id == me), None)
    can_vote = role >= Role.DEVELOPER
    lead_needed = d.needs_lead_approval and not d.lead_approved_by
    as_lead = lead_needed and role >= Role.LEAD and not am_author
    as_participant = mine is not None and mine.decision != "approve"
    approve = (open_ and d.state == ProposalState.AWAITING_CONSENT and can_vote and (as_participant or as_lead))
    reject = open_ and can_vote and (mine is not None or role >= Role.LEAD)
    cancel = open_ and can_vote and (am_author or role >= Role.ADMIN)
    resolve = open_ and can_vote and (am_author or role >= Role.ADMIN)
    force = open_ and role >= Role.ADMIN and d.n_unresolved == 0 and pending_resolutions == 0
    return Permissions(approve=approve, reject=reject, cancel=cancel, resolve=resolve, force=force,
                       read_only=role < Role.DEVELOPER, approve_as_lead=approve and as_lead)


# ======================================================================================= modifiche
class ChangeBrowser(QWidget):
    """Albero delle modifiche senza conflitto + diff Ladder della voce selezionata."""

    def __init__(self) -> None:
        super().__init__()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Elemento", "Tipo", "Origine"])
        self.tree.setColumnWidth(0, 310)
        self.tree.setColumnWidth(1, 90)
        self.tree.setRootIsDecorated(True)
        self.path = QLabel()
        self.path.setProperty("muted", True)
        self.path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.diff = DiffViewer()
        right = QWidget()
        lay = QVBoxLayout(right)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.addWidget(self.path)
        lay.addWidget(self.diff, 1)
        sp = QSplitter()
        sp.addWidget(self.tree)
        sp.addWidget(right)
        sp.setStretchFactor(1, 3)
        sp.setSizes([520, 700])
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.addWidget(sp)
        self.tree.currentItemChanged.connect(self._selected)

    def set_changes(self, author: str, mine: list[ChangeOut], upstream: list[ChangeOut]) -> None:
        self.tree.clear()
        self.diff.set_diff("", "")
        self.path.setText("")
        for title, items in ((f"Modifiche proposte da {author} ({len(mine)})", mine),
                             (f"Già presenti nel Master dopo la base ({len(upstream)})", upstream)):
            top = QTreeWidgetItem([title])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(top)
            for ch in items:
                it = QTreeWidgetItem([ch.path, KIND_IT[ch.kind], ORIGIN_IT[ch.origin]])
                it.setData(0, Qt.UserRole, ch)
                it.setForeground(1, QBrush(QColor(KIND_COLOR[ch.kind])))
                top.addChild(it)
            top.setExpanded(True)
        first = self.tree.topLevelItem(0)
        if first is not None and first.childCount():
            self.tree.setCurrentItem(first.child(0))

    def _selected(self, cur: Optional[QTreeWidgetItem], _prev=None) -> None:     # noqa: ANN001
        ch: Optional[ChangeOut] = cur.data(0, Qt.UserRole) if cur else None
        if ch is None:
            return
        self.path.setText(ch.path)
        self.diff.set_diff(ch.base_text, ch.new_text, "Base", f"Dopo ({ORIGIN_IT[ch.origin]})")


# ======================================================================================= conflitti
class ConflictPanel(QWidget):
    """Tre pannelli Base | Proposta | Master + scelta di risoluzione per un conflitto."""

    choice_changed = Signal(str, object)         # conflict id, ResolutionIn

    def __init__(self) -> None:
        super().__init__()
        self._conflict: Optional[ConflictOut] = None
        self._loading = False
        self.title = QLabel()
        self.title.setStyleSheet("font-weight:700; font-size:14px;")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.kind = QLabel()
        self.kind.setProperty("muted", True)
        self.view = ThreeWayView()
        self.rb = {k: QRadioButton(t) for k, t in (("mine", "Tieni la proposta"), ("theirs", "Tieni il Master"),
                                                   ("both", "Tieni entrambe (Master poi proposta)"),
                                                   ("custom", "Modifica manualmente"))}
        self.group = QButtonGroup(self)
        row = QHBoxLayout()
        row.setSpacing(14)
        lab = QLabel("Risoluzione")
        lab.setStyleSheet("font-weight:600;")
        row.addWidget(lab)
        for r in self.rb.values():
            self.group.addButton(r)
            r.toggled.connect(self._changed)
            row.addWidget(r)
        row.addStretch(1)
        self.editor = QPlainTextEdit()
        self.editor.setProperty("mono", True)
        self.editor.setFixedHeight(110)
        self.editor.textChanged.connect(self._changed)
        self.hint = QLabel()
        self.hint.setProperty("muted", True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.addWidget(self.title)
        lay.addWidget(self.kind)
        lay.addWidget(self.view, 1)
        lay.addLayout(row)
        lay.addWidget(self.hint)
        lay.addWidget(self.editor)
        self.editor.hide()
        self.hint.hide()

    def show_conflict(self, c: ConflictOut, base_rev: int, author: str, head_rev: int,
                      choice: Optional[ResolutionIn], editable: bool) -> None:
        self._loading = True
        self._conflict = c
        self.title.setText(c.path)
        self.kind.setText({"modify/modify": "Entrambi hanno modificato lo stesso elemento",
                           "modify/delete": "Uno ha modificato, l'altro ha eliminato",
                           "add/add": "Entrambi hanno aggiunto lo stesso elemento in modo diverso"}.get(c.kind, c.kind))
        self.view.set_texts(c.base_text, c.mine_text, c.theirs_text,
                            (f"Base (rev {base_rev})", f"Proposta di {author}", f"Master (rev {head_rev})"))
        self.rb["both"].setVisible(c.sequence)
        self.hint.setText("Una rung / riga per linea (testo del Rung, senza commenti)." if c.sequence
                          else "Incolla l'XML completo dell'elemento risolto.")
        self.group.setExclusive(False)
        for r in self.rb.values():
            r.setChecked(False)
            r.setEnabled(editable)
        self.group.setExclusive(True)
        self.editor.setReadOnly(not editable)
        sel = choice.choice if choice else (c.choice if c.resolved else None)
        if sel in self.rb:
            self.rb[sel].setChecked(True)
        self.editor.setPlainText("\n".join(choice.custom or []) if (choice and choice.custom)
                                 else (c.mine_text if sel != "theirs" else c.theirs_text))
        self.editor.setVisible(sel == "custom")
        self.hint.setVisible(sel == "custom")
        self._loading = False

    def _changed(self) -> None:
        if self._loading or self._conflict is None:
            return
        sel = next((k for k, r in self.rb.items() if r.isChecked()), None)
        self.editor.setVisible(sel == "custom")
        self.hint.setVisible(sel == "custom")
        if sel is None:
            return
        custom = None
        if sel == "custom":
            txt = self.editor.toPlainText()
            custom = [ln for ln in txt.splitlines() if ln.strip()] if self._conflict.sequence else [txt]
        self.choice_changed.emit(self._conflict.id, ResolutionIn(choice=sel, custom=custom))


# ======================================================================================= finestra
class ApprovalDialog(QDialog):
    """Finestra di consenso per una proposta di merge."""

    proposal_changed = Signal(object)            # ProposalDetail aggiornato (per la finestra principale)

    def __init__(self, api: ApiClient, proposal_id: str, my_role: Optional[Role] = None,
                 run: Callable = run_async, parent: Optional[QWidget] = None, master_ext: str = ".L5X",
                 master_name: str = "Master"):
        super().__init__(parent)
        self.api, self.pid, self._run = api, proposal_id, run
        self.master_ext = master_ext or ".L5X"
        self.master_name = master_name or "Master"
        self.me = api.session_key
        self.role = my_role if my_role is not None else api.role
        self.detail: Optional[ProposalDetail] = None
        self.perms: Optional[Permissions] = None
        self._pending: dict[str, ResolutionIn] = {}
        self.setWindowTitle("Richiesta di Merge · Consenso")
        self.resize(1240, 800)

        # ------------------------------------------------------------ intestazione
        head = QFrame()
        head.setObjectName("Panel")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.avatar_box = QHBoxLayout()
        self.avatar_box.setContentsMargins(0, 0, 0, 0)
        top.addLayout(self.avatar_box)
        col = QVBoxLayout()
        col.setSpacing(2)
        trow = QHBoxLayout()
        trow.setSpacing(8)
        self.lbl_title = QLabel("Richiesta di merge")
        self.lbl_title.setProperty("h2", True)
        self.role_box = QHBoxLayout()
        trow.addWidget(self.lbl_title)
        trow.addLayout(self.role_box)
        trow.addStretch(1)
        col.addLayout(trow)
        self.lbl_meta = QLabel("")
        self.lbl_meta.setProperty("muted", True)
        col.addWidget(self.lbl_meta)
        top.addLayout(col, 1)
        self.badge = StatusBadge("pending")
        top.addWidget(self.badge, 0, Qt.AlignTop)
        hl.addLayout(top)
        self.lbl_msg = QLabel("")
        self.lbl_msg.setWordWrap(True)
        hl.addWidget(self.lbl_msg)
        hl.addWidget(h_line())
        self.people = QHBoxLayout()
        self.people.setSpacing(18)
        hl.addLayout(self.people)

        # ------------------------------------------------------------ schede
        self.changes = ChangeBrowser()
        self.conf_list = QListWidget()
        self.conf_panel = ConflictPanel()
        csp = QSplitter()
        csp.addWidget(self.conf_list)
        csp.addWidget(self.conf_panel)
        csp.setStretchFactor(1, 4)
        csp.setSizes([280, 940])
        self.warn = QPlainTextEdit()
        self.warn.setReadOnly(True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.changes, "Modifiche")
        self.tabs.addTab(csp, "Conflitti")
        self.tabs.addTab(self.warn, "Avvisi")

        # ------------------------------------------------------------ piede
        self.btn_apply = QPushButton("Applica risoluzioni")
        self.btn_apply.setProperty("variant", "warning")
        self.btn_preview = QPushButton("Salva anteprima...")
        self.btn_cancel = QPushButton("Annulla richiesta")
        self.btn_force = QPushButton("Forza Merge...")
        self.btn_force.setProperty("variant", "warning")
        self.btn_reject = QPushButton("Rifiuta / Chiedi Modifiche")
        self.btn_reject.setProperty("variant", "danger")
        self.btn_approve = QPushButton("Dai Consenso al Merge")
        self.btn_approve.setProperty("variant", "success")
        self.btn_close = QPushButton("Chiudi")
        for b in (self.btn_reject, self.btn_approve):
            b.setMinimumHeight(36)
        self.status = QLabel("")
        self.status.setProperty("muted", True)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        for b in (self.btn_apply, self.btn_preview, self.btn_cancel, self.btn_force):
            foot.addWidget(b)
        foot.addWidget(self.status, 1)
        for b in (self.btn_reject, self.btn_approve, self.btn_close):
            foot.addWidget(b)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(head)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(foot)

        self.conf_list.currentRowChanged.connect(self._show_conflict)
        self.conf_panel.choice_changed.connect(self._choice)
        self.btn_apply.clicked.connect(self._apply)
        self.btn_preview.clicked.connect(lambda: self._save_preview())
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_force.clicked.connect(self._force)
        self.btn_reject.clicked.connect(self._reject)
        self.btn_approve.clicked.connect(self._approve)
        self.btn_close.clicked.connect(self.close)
        self._avatar: Optional[Avatar] = None
        self.reload()

    # ------------------------------------------------------------------ caricamento
    def reload(self) -> None:
        self._run(lambda: self.api.proposal(self.pid), self.populate, self._error)

    def on_event(self, ev: dict) -> None:
        p = ev.get("proposal")
        if p and p.get("id") == self.pid:
            self.reload()

    @staticmethod
    def _clear(layout: QHBoxLayout) -> None:
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w:
                drop_widget(w)

    def populate(self, d: ProposalDetail) -> None:
        self.detail = d
        self._pending = {k: v for k, v in self._pending.items() if any(c.id == k for c in d.conflicts)}
        self.perms = compute_permissions(d, self.me, self.role, len(self._pending))
        pm = self.perms
        open_ = d.state in OPEN_STATES

        self._clear(self.avatar_box)
        self.avatar_box.addWidget(Avatar(d.author, 46))
        self._clear(self.role_box)
        if d.author_role:
            self.role_box.addWidget(role_chip(d.author_role))
        self.lbl_title.setText(f"Richiesta di merge di {d.author}")
        res = f"  ·  → Master rev {d.result_revision}" if d.result_revision else ""
        self.lbl_meta.setText(f"{human_time(d.created_at)}  ·  base rev {d.base_revision}  ·  Master analizzato rev "
                              f"{d.head_revision}{res}  ·  {d.n_changes} modifiche  ·  "
                              f"{d.n_unresolved}/{d.n_conflicts} conflitti da risolvere  ·  #{d.id}")
        self.lbl_msg.setText(d.message)
        self.lbl_msg.setVisible(bool(d.message))
        all_ok = bool(d.participants) and all(p.decision == "approve" for p in d.participants)
        self.badge.set_kind(badge_kind(d.state, needs_lead_pending=(d.needs_lead_approval and not d.lead_approved_by
                                                                    and all_ok), forced=bool(d.forced_by)))
        self._fill_people(d)

        self.changes.set_changes(d.author, list(d.changes), list(d.upstream_changes))
        self.tabs.setTabText(0, f"Modifiche ({len(d.changes)})")
        self.tabs.setTabText(1, f"Conflitti ({d.n_unresolved}/{d.n_conflicts})")
        self._fill_conflicts()
        self.warn.setPlainText("\n".join(d.warnings) or "Nessun avviso.")
        self.tabs.setTabText(2, f"Avvisi ({len(d.warnings)})")
        if d.conflicts and d.n_unresolved:
            self.tabs.setCurrentIndex(1)

        self.btn_approve.setEnabled(pm.approve)
        self.btn_approve.setText("Dai Consenso Finale (Lead)" if pm.approve_as_lead else "Dai Consenso al Merge")
        self.btn_reject.setEnabled(pm.reject)
        self.btn_cancel.setEnabled(pm.cancel)
        self.btn_apply.setEnabled(pm.resolve and bool(self._pending))
        self.btn_apply.setVisible(bool(d.conflicts) and pm.resolve)
        self.btn_force.setVisible(self.role >= Role.ADMIN)
        self.btn_force.setEnabled(pm.force)
        self.btn_force.setToolTip("" if pm.force else "Risolvi (e applica) tutti i conflitti prima di forzare il merge")
        self.btn_reject.setVisible(not pm.read_only)
        self.btn_approve.setVisible(not pm.read_only)
        self.btn_cancel.setVisible(not pm.read_only)
        for b in (self.btn_approve, self.btn_reject, self.btn_force, self.btn_apply):
            polish(b)
        self.status.setText("Sola lettura: il ruolo Viewer non può votare." if (pm.read_only and open_)
                            else "" if open_ else "Richiesta chiusa.")
        self.proposal_changed.emit(d)

    def _fill_people(self, d: ProposalDetail) -> None:
        self._clear(self.people)
        for p in d.participants:
            box = QHBoxLayout()
            box.setSpacing(8)
            box.addWidget(Avatar(p.developer, 30, ring=DECISION_RING[p.decision]))
            c = QVBoxLayout()
            c.setSpacing(0)
            n = QLabel(p.developer + (" (autore)" if p.is_author else ""))
            n.setStyleSheet("font-weight:600;")
            s = QLabel(DECISION_TEXT[p.decision] + ("" if p.online else " · offline"))
            s.setStyleSheet(f"color:{DECISION_RING[p.decision]}; font-size:11px;")
            if p.comment:
                s.setToolTip(p.comment)
                s.setText(s.text() + f" · «{p.comment[:40]}»")
            c.addWidget(n)
            c.addWidget(s)
            box.addLayout(c)
            holder = QWidget()
            holder.setLayout(box)
            self.people.addWidget(holder)
        if d.needs_lead_approval:
            self.people.addWidget(chip(f"Consenso Finale Lead ✔ {d.lead_approved_by}", T["GREEN"])
                                  if d.lead_approved_by else chip("Serve il Consenso Finale di un Lead", T["ORANGE"]))
        if d.forced_by:
            self.people.addWidget(chip(f"Merge forzato da {d.forced_by}", T["ORANGE"]))
        self.people.addStretch(1)

    # ------------------------------------------------------------------ conflitti
    def _fill_conflicts(self) -> None:
        d = self.detail
        assert d is not None
        row = self.conf_list.currentRow()
        self.conf_list.blockSignals(True)
        self.conf_list.clear()
        for c in d.conflicts:
            pend = self._pending.get(c.id)
            tag = (f"scelta: {CHOICE_IT[pend.choice]}" if pend else
                   f"risolto: {CHOICE_IT.get(c.choice or '', '')}" if c.resolved else "DA RISOLVERE")
            done = c.resolved or bool(pend)
            it = QListWidgetItem(f"{'✔' if done else '⚠'} {c.title}\n    {tag}")
            it.setForeground(QBrush(QColor(T["GREEN"] if done else T["RED"])))
            self.conf_list.addItem(it)
        self.conf_list.blockSignals(False)
        if d.conflicts:
            self.conf_list.setCurrentRow(max(0, min(row, len(d.conflicts) - 1)))
            self._show_conflict(self.conf_list.currentRow())

    def _show_conflict(self, row: int) -> None:
        d = self.detail
        if d is None or self.perms is None or not (0 <= row < len(d.conflicts)):
            return
        c = d.conflicts[row]
        self.conf_panel.show_conflict(c, d.base_revision, d.author, d.head_revision, self._pending.get(c.id),
                                      self.perms.resolve)

    def _choice(self, cid: str, res: ResolutionIn) -> None:
        self._pending[cid] = res
        d = self.detail
        if d is not None:
            self.perms = compute_permissions(d, self.me, self.role, len(self._pending))
            self.btn_apply.setEnabled(self.perms.resolve)
            self.btn_force.setEnabled(self.perms.force)
        row = self.conf_list.currentRow()
        self._fill_conflicts()
        self.conf_list.setCurrentRow(row)

    def _apply(self) -> None:
        if not self._pending:
            return
        sent = dict(self._pending)
        self.status.setText("Applico le risoluzioni...")

        def ok(d: ProposalDetail) -> None:
            for k in sent:
                self._pending.pop(k, None)
            self.populate(d)
        self._run(lambda: self.api.set_resolutions(self.pid, sent), ok, self._error)

    # ------------------------------------------------------------------ azioni
    def _approve(self) -> None:
        d, pm = self.detail, self.perms
        if d is None or pm is None:
            return
        waiting = [p.developer for p in d.participants if p.decision != "approve" and p.session_id != self.me]
        if pm.approve_as_lead:
            msg = (f"Dai il Consenso Finale come Lead al merge di {d.n_changes} modifiche di {d.author}?\n\n"
                   "Il Master verrà aggiornato appena tutti i partecipanti avranno dato il consenso.")
        else:
            msg = (f"Confermi il consenso al merge di {d.n_changes} modifiche di {d.author}?\n\n"
                   + (f"Il Master verrà aggiornato solo dopo il consenso di: {', '.join(waiting)}." if waiting
                      else "Sei l'ultimo a dover confermare: il Master verrà aggiornato subito."))
        if QMessageBox.question(self, "Conferma e Sincronizza", msg) != QMessageBox.Yes:
            return
        self.status.setText("Invio consenso...")
        self._run(lambda: self.api.approve(self.pid), self.populate, self._error)

    def _reject(self) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Rifiuta / Chiedi Modifiche",
                                                 "Motivo e modifiche richieste (visibile a tutti):")
        if not ok:
            return
        if not text.strip():
            QMessageBox.warning(self, "Commento richiesto", "Indica il motivo del rifiuto.")
            return
        self._run(lambda: self.api.reject(self.pid, text.strip()), self.populate, self._error)

    def _cancel(self) -> None:
        if QMessageBox.question(self, "Annulla richiesta", "Annullare questa richiesta di merge?") == QMessageBox.Yes:
            self._run(lambda: self.api.cancel(self.pid), self.populate, self._error)

    def _force(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "Forza Merge (Admin)",
            "Il merge verrà applicato al Master senza attendere i consensi.\n"
            "Indica il motivo (minimo 5 caratteri): finirà nel registro eventi.")
        if not ok:
            return
        if len(text.strip()) < 5:
            QMessageBox.warning(self, "Motivo richiesto", "Il motivo deve avere almeno 5 caratteri.")
            return
        self._run(lambda: self.api.force_merge(self.pid, text.strip()), self.populate, self._error)

    def _save_preview(self, path: Optional[str] = None) -> None:
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Salva anteprima", f"{self.master_name}_anteprima{self.master_ext}",
                                                  f"File del progetto (*{self.master_ext});;Tutti i file (*)")
        if path:
            self._run(lambda: self.api.save_preview(self.pid, Path(path)),
                      lambda _x: self.status.setText(f"Anteprima salvata in {path}"), self._error)

    def _error(self, exc: Exception) -> None:
        self.status.setText("")
        QMessageBox.warning(self, "Errore", exc.detail if isinstance(exc, ApiError) else str(exc))
        self.reload()
