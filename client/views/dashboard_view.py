"""Dashboard di Merge & Consenso: schede con avatar, timestamp, routine modificate e badge di stato animati."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from client.theme.tokens import FONT_MONO, T
from client.theme.widgets import Avatar, StatusBadge, badge_kind, icon, role_color
from client.views.common import (chip, drop_widget, full_time, h_line, heading, human_time, item, make_table, muted,
                                 role_chip, short_unit)
from common.roles import Role
from common.schemas import ACTIVE_STATES, DeveloperOut, MasterInfo, ParticipantOut, ProposalSummary

RING = {"approve": T["GREEN"], "reject": T["RED"], "pending": T["ORANGE"]}
RING_TEXT = {"approve": "ha dato il consenso", "reject": "ha rifiutato", "pending": "in attesa"}


# ======================================================================================= scheda proposta
class ProposalCard(QFrame):
    """Scheda cliccabile di una richiesta di merge."""

    clicked = Signal(str)

    def __init__(self, p: ProposalSummary, mine: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.proposal = p
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        # intestazione: avatar, autore + ruolo, timestamp, badge
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(Avatar(p.author, 40))
        who = QVBoxLayout()
        who.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name = QLabel(p.author + ("  (tu)" if mine else ""))
        name.setStyleSheet("font-weight:700; font-size:14px;")
        name_row.addWidget(name)
        if p.author_role:
            name_row.addWidget(role_chip(p.author_role))
        name_row.addStretch(1)
        who.addLayout(name_row)
        when = QLabel(f"{human_time(p.created_at)}  ·  rev {p.base_revision} → {p.head_revision}  ·  #{p.id}")
        when.setProperty("muted", True)
        when.setToolTip(f"Creata: {full_time(p.created_at)}\nAggiornata: {full_time(p.updated_at)}")
        who.addWidget(when)
        top.addLayout(who, 1)
        pending_lead = p.needs_lead_approval and not p.lead_approved_by
        all_ok = bool(p.participants) and all(x.decision == "approve" for x in p.participants)
        self.badge = StatusBadge(badge_kind(p.state, needs_lead_pending=pending_lead and all_ok,
                                            forced=bool(p.forced_by)))
        top.addWidget(self.badge, 0, Qt.AlignTop)
        lay.addLayout(top)

        if p.message:
            msg = QLabel(p.message)
            msg.setWordWrap(True)
            msg.setStyleSheet(f"color:{T['TEXT']};")
            lay.addWidget(msg)

        # routine / elementi modificati
        if p.touched:
            row = QHBoxLayout()
            row.setSpacing(6)
            for t in p.touched[:4]:
                c = chip(short_unit(t), mono=True, max_chars=26)
                c.setToolTip(t)
                row.addWidget(c)
            if len(p.touched) > 4:
                more = chip(f"+{len(p.touched) - 4}", mono=True)
                more.setToolTip("\n".join(p.touched[4:]))
                row.addWidget(more)
            row.addStretch(1)
            lay.addLayout(row)

        # piede: partecipanti con anello di stato, metriche, consenso del Lead
        foot = QHBoxLayout()
        foot.setSpacing(4)
        for x in p.participants:
            av = Avatar(x.developer, 28, ring=RING[x.decision])
            av.setToolTip(f"{x.developer}: {RING_TEXT[x.decision]}" + ("" if x.online else " (offline)")
                          + (f"\n«{x.comment}»" if x.comment else ""))
            foot.addWidget(av)
        if p.participants:
            ok = sum(1 for x in p.participants if x.decision == "approve")
            lab = QLabel(f"  {ok}/{len(p.participants)} consensi")
            lab.setProperty("muted", True)
            foot.addWidget(lab)
        foot.addStretch(1)
        if p.needs_lead_approval:
            foot.addWidget(chip(f"Lead ✔ {p.lead_approved_by}", T["GREEN"]) if p.lead_approved_by
                           else chip("Serve consenso Lead", T["ORANGE"]))
        if p.forced_by:
            foot.addWidget(chip(f"Forzato da {p.forced_by}", T["ORANGE"]))
        foot.addWidget(chip(f"{p.n_changes} modifiche"))
        if p.n_conflicts:
            foot.addWidget(chip(f"{p.n_unresolved}/{p.n_conflicts} conflitti", T["RED"] if p.n_unresolved else T["GREEN"]))
        lay.addLayout(foot)

    def mousePressEvent(self, e) -> None:                            # noqa: ANN001
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.proposal.id)
        super().mousePressEvent(e)


# ======================================================================================= cronologia Master
class HistoryDialog(QDialog):
    """Revisioni del Master: download (tutti) e ripristino (solo Admin)."""

    download_requested = Signal(int)
    restore_requested = Signal(int)

    def __init__(self, history: list[MasterInfo], role: Role, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Storico revisioni del Master")
        self.resize(780, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.addWidget(heading("Storico revisioni", 2))
        self.table = make_table(["Rev", "Data", "Autore", "Messaggio"], stretch_col=3)
        lay.addWidget(self.table, 1)
        self._rows = sorted(history, key=lambda m: -m.revision)
        self.table.setRowCount(len(self._rows))
        for r, m in enumerate(self._rows):
            self.table.setItem(r, 0, item(f"rev {m.revision}", T["ACCENT"], data=m.revision, bold=True))
            self.table.setItem(r, 1, item(full_time(m.updated_at)))
            self.table.setItem(r, 2, item(m.updated_by))
            self.table.setItem(r, 3, item(m.message))
        if self._rows:
            self.table.selectRow(0)
        btns = QHBoxLayout()
        self.btn_dl = QPushButton("  Scarica revisione")
        self.btn_dl.setIcon(icon("download", T["TEXT"], 18))
        self.btn_restore = QPushButton("Ripristina come nuova revisione...")
        self.btn_restore.setProperty("variant", "warning")
        self.btn_restore.setVisible(role >= Role.ADMIN)
        close = QPushButton("Chiudi")
        btns.addWidget(self.btn_dl)
        btns.addWidget(self.btn_restore)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)
        self.btn_dl.clicked.connect(lambda: self._emit(self.download_requested))
        self.btn_restore.clicked.connect(lambda: self._emit(self.restore_requested))
        close.clicked.connect(self.accept)

    def selected_revision(self) -> Optional[int]:
        r = self.table.currentRow()
        return self._rows[r].revision if 0 <= r < len(self._rows) else None

    def _emit(self, sig: Signal) -> None:
        rev = self.selected_revision()
        if rev is not None:
            sig.emit(rev)


# ======================================================================================= dashboard
class DashboardView(QWidget):
    open_proposal = Signal(str)
    checkout_requested = Signal()
    merge_requested = Signal()
    history_requested = Signal()
    refresh_requested = Signal()
    folder_change_requested = Signal()
    folder_open_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._proposals: list[ProposalSummary] = []
        self._me = ""
        self._role = Role.VIEWER
        self._only_active = True

        root = QHBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(20)

        # ---------------------------------------------------------------- colonna principale
        main = QVBoxLayout()
        main.setSpacing(12)
        self.title = heading("Merge & Consenso")
        self.lbl_master = QLabel("Nessun progetto selezionato")
        self.lbl_master.setTextFormat(Qt.RichText)
        self.lbl_local = QLabel("")
        self.lbl_local.setTextFormat(Qt.RichText)
        self.lbl_local.setProperty("muted", True)
        main.addWidget(self.title)
        main.addWidget(self.lbl_master)
        main.addWidget(self.lbl_local)

        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.lbl_folder = QLabel("")
        self.lbl_folder.setTextFormat(Qt.RichText)
        self.lbl_folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_folder = QPushButton("  Cambia cartella...")
        self.btn_folder.setIcon(icon("folder", T["TEXT"], 16))
        self.btn_folder.setToolTip("Scegli dove salvare il progetto in locale: e' anche la cartella in cui "
                                   "«Richiedi Merge» cerca il tuo file (stessa estensione del Master)")
        self.btn_open_folder = QPushButton("Apri")
        self.btn_open_folder.setProperty("variant", "ghost")
        frow.addWidget(self.lbl_folder, 1)
        frow.addWidget(self.btn_folder)
        frow.addWidget(self.btn_open_folder)
        main.addLayout(frow)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_checkout = QPushButton("  Checkout ultimo Master")
        self.btn_checkout.setIcon(icon("download", T["TEXT"], 18))
        self.btn_merge = QPushButton("  Richiedi Merge...")
        self.btn_merge.setProperty("variant", "primary")
        self.btn_merge.setIcon(icon("upload", T["TEXT_ON_ACCENT"], 18))
        self.btn_history = QPushButton("  Storico")
        self.btn_history.setIcon(icon("history", T["TEXT"], 18))
        self.btn_refresh = QPushButton()
        self.btn_refresh.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_refresh.setToolTip("Aggiorna")
        self.btn_refresh.setProperty("variant", "ghost")
        for b in (self.btn_checkout, self.btn_merge, self.btn_history):
            bar.addWidget(b)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        self.btn_active, self.btn_all = QPushButton("Attive"), QPushButton("Tutte")
        grp = QButtonGroup(self)
        for b in (self.btn_active, self.btn_all):
            b.setCheckable(True)
            b.setProperty("seg", True)
            grp.addButton(b)
            bar.addWidget(b)
        self.btn_active.setChecked(True)
        main.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.holder = QWidget()
        self.holder.setStyleSheet("background:transparent;")
        self.cards = QVBoxLayout(self.holder)
        self.cards.setContentsMargins(0, 0, 8, 0)
        self.cards.setSpacing(12)
        self.cards.addStretch(1)
        self.scroll.setWidget(self.holder)
        self.empty = muted("Nessuna richiesta di merge. Esegui il Checkout, modifica il file offline e premi "
                           "«Richiedi Merge».")
        self.empty.setAlignment(Qt.AlignCenter)
        main.addWidget(self.scroll, 1)
        root.addLayout(main, 1)

        # ---------------------------------------------------------------- colonna destra
        side = QFrame()
        side.setObjectName("Panel")
        side.setFixedWidth(280)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.addWidget(heading("Programmatori", 2))
        self.devs = QVBoxLayout()
        self.devs.setSpacing(8)
        sl.addLayout(self.devs)
        sl.addWidget(h_line())
        sl.addWidget(heading("Eventi", 2))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        sl.addWidget(self.log, 1)
        root.addWidget(side)

        self.btn_checkout.clicked.connect(self.checkout_requested)
        self.btn_merge.clicked.connect(self.merge_requested)
        self.btn_history.clicked.connect(self.history_requested)
        self.btn_refresh.clicked.connect(self.refresh_requested)
        self.btn_folder.clicked.connect(self.folder_change_requested)
        self.btn_open_folder.clicked.connect(self.folder_open_requested)
        self.btn_active.clicked.connect(lambda: self._set_filter(True))
        self.btn_all.clicked.connect(lambda: self._set_filter(False))
        self.set_identity("", Role.VIEWER)
        self._rebuild()

    # ------------------------------------------------------------------ API
    def set_identity(self, session_key: str, role: Role) -> None:
        """Adegua i pulsanti al ruolo: il Viewer scarica soltanto, non puo' inviare modifiche."""
        self._me, self._role = session_key, role
        self.btn_merge.setVisible(role >= Role.DEVELOPER)
        self.btn_checkout.setText("  Checkout ultimo Master" if role >= Role.DEVELOPER else "  Scarica ultimo Master")
        self._rebuild()

    def set_enabled_project(self, on: bool) -> None:
        for b in (self.btn_checkout, self.btn_merge, self.btn_history, self.btn_refresh, self.btn_folder,
                  self.btn_open_folder):
            b.setEnabled(on)

    def set_master(self, project_name: str, m: Optional[MasterInfo]) -> None:
        self.title.setText(project_name or "Merge & Consenso")
        if m is None:
            self.lbl_master.setText(f"<span style='color:{T['ORANGE']}'>Master non ancora inizializzato</span> "
                                    "— un Lead o un Admin deve caricare il file Master (L5K, L5X o qualsiasi altro file).")
            return
        self.lbl_master.setText(
            f"<span style='color:{T['ACCENT']}; font-weight:700'>Master rev {m.revision}</span> &nbsp; "
            f"<span style='color:{T['MUTED']}'>aggiornato da {m.updated_by} · {human_time(m.updated_at)}"
            f"{' · ' + m.message if m.message else ''}</span>")

    def set_local(self, text: str) -> None:
        self.lbl_local.setText(text)

    def set_folder(self, path: str, chosen: bool) -> None:
        """Mostra la cartella locale del progetto; ``chosen=False`` indica che e' ancora quella proposta di default."""
        note = "" if chosen else f" <span style='color:{T['ORANGE']}'>(proposta: scegline una con «Cambia cartella»)</span>"
        self.lbl_folder.setText(f"<span style='color:{T['MUTED']}'>Cartella locale:</span> "
                                f"<span style='font-family:{FONT_MONO}'>{path}</span>{note}")
        self.lbl_folder.setToolTip(path)

    def set_proposals(self, items: list[ProposalSummary]) -> None:
        self._proposals = items
        self._rebuild()

    def set_developers(self, devs: list[DeveloperOut]) -> None:
        while self.devs.count():
            w = self.devs.takeAt(0).widget()
            if w:
                drop_widget(w)
        for d in devs:
            row = QWidget()
            row.setObjectName("cell")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            rl.addWidget(Avatar(d.developer, 28, ring=T["GREEN"] if d.connected else None))
            col = QVBoxLayout()
            col.setSpacing(0)
            n = QLabel(d.developer)
            n.setStyleSheet("font-weight:600;")
            role = d.role or ""
            sub = QLabel(f"{role.replace('_', ' ')} · base rev {d.checked_out_revision}" if d.checked_out_revision
                         else role.replace("_", " "))
            sub.setStyleSheet(f"color:{role_color(role) if role else T['MUTED']}; font-size:11px;")
            col.addWidget(n)
            col.addWidget(sub)
            rl.addLayout(col, 1)
            self.devs.addWidget(row)
        if not devs:
            self.devs.addWidget(muted("Nessuno connesso"))

    def log_event(self, msg: str) -> None:
        if msg:
            self.log.appendPlainText(msg)

    # ------------------------------------------------------------------ interno
    def _set_filter(self, only_active: bool) -> None:
        self._only_active = only_active
        self._rebuild()

    def visible_proposals(self) -> list[ProposalSummary]:
        items = [p for p in self._proposals if (p.state in ACTIVE_STATES or not self._only_active)]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    def _rebuild(self) -> None:
        while self.cards.count() > 1:
            w = self.cards.takeAt(0).widget()
            if w is self.empty:
                w.hide()                                  # riusato: non va distrutto
            elif w:
                drop_widget(w)
        items = self.visible_proposals()
        for p in items:
            card = ProposalCard(p, mine=(p.author_session == self._me))
            card.clicked.connect(self.open_proposal)
            self.cards.insertWidget(self.cards.count() - 1, card)
        if not items:
            self.cards.insertWidget(0, self.empty)
            self.empty.show()
        else:
            self.empty.hide()
