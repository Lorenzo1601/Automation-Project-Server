"""Dashboard di Merge & Consenso: schede con avatar, timestamp, routine modificate e badge di stato animati."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from client.branch_info import MAIN_LABEL, abilities, behind_text, kind_label, state_label
from client.theme.tokens import FONT_MONO, T
from client.theme.widgets import (Avatar, EmptyState, StatusBadge, badge_color, badge_kind, icon, icon_pixmap,
                                  role_color)
from client.views.common import (chip, drop_widget, eyebrow, full_time, h_line, heading, human_time, item, make_table,
                                 muted, role_chip, short_unit, tint)
from common.roles import Role
from common.schemas import ACTIVE_STATES, DeveloperOut, MasterInfo, PresenceOut, ParticipantOut, ProposalSummary
from common.schemas_branches import BranchOut
from common.schemas_revisions import LockOut

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
        self.setProperty("clickable", True)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 16, 18, 14)
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
        self._stripe = QColor(badge_color(self.badge.kind))          # barra di stato sul bordo sinistro
        lay.addLayout(top)

        if p.message:
            msg = QLabel(p.message)
            msg.setWordWrap(True)
            msg.setStyleSheet(f"color:{T['TEXT']}; font-size:14px;")
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
        if p.n_comments:                                             # discussione in corso
            cc = chip(f"💬 {p.n_comments}", T["ACCENT"])
            cc.setToolTip(f"{p.n_comments} commenti nella discussione")
            foot.addWidget(cc)
        foot.addWidget(chip(f"{p.n_changes} modifiche"))
        if p.n_conflicts:
            foot.addWidget(chip(f"{p.n_unresolved}/{p.n_conflicts} conflitti", T["RED"] if p.n_unresolved else T["GREEN"]))
        lay.addLayout(foot)

    def paintEvent(self, e) -> None:                                 # noqa: ANN001
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._stripe)
        p.drawRoundedRect(QRectF(1.5, 14, 4, max(8, self.height() - 28)), 2, 2)

    def mousePressEvent(self, e) -> None:                            # noqa: ANN001
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.proposal.id)
        super().mousePressEvent(e)


# ======================================================================================= dashboard
class DashboardView(QWidget):
    open_proposal = Signal(str)
    checkout_requested = Signal()
    merge_requested = Signal()
    history_requested = Signal()
    import_master_requested = Signal()
    reimport_master_requested = Signal()
    refresh_requested = Signal()
    folder_change_requested = Signal()
    folder_open_requested = Signal()
    lock_take_requested = Signal()              # «Prendi in carico»
    lock_release_requested = Signal()           # «Rilascia»
    lock_force_requested = Signal()             # «Forza rilascio...» (solo Admin)
    checkin_requested = Signal()                # «Check-in nuova revisione...» (solo progetti binari)
    branches_requested = Signal()               # «Branch...» (elenco e gestione dei branch)
    commit_requested = Signal()                 # «Salva su branch...» (branch personale)
    branch_sync_requested = Signal()            # «Aggiorna dal principale» (banner del branch)
    branch_merge_requested = Signal()           # «Unisci nel principale...» (banner del branch)
    branch_leave_requested = Signal()           # «Torna al principale» (banner del branch)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._project_name = ""                          # nome del progetto mostrato nel titolo
        self._branch_slug: Optional[str] = None          # branch corrente (None = principale)
        self._branch: Optional[BranchOut] = None         # sua descrizione, se gia' nota
        self._merge_allowed = True                       # «Richiedi Merge» consentito dal progetto (Master presente, non archiviato)
        self._proposals: list[ProposalSummary] = []
        self._me = ""
        self._role = Role.VIEWER
        self._only_active = True
        self._project_on = False                         # un progetto e' selezionato
        self._master_init: Optional[bool] = None         # Master inizializzato? (None = non noto)

        root = QHBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(24)

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

        # ---- banner del branch: visibile solo quando si lavora su un branch (contesto sempre esplicito)
        self.branch_bar = QFrame()
        self.branch_bar.setObjectName("BranchBar")
        bbr = QHBoxLayout(self.branch_bar)
        bbr.setContentsMargins(14, 8, 8, 8)
        bbr.setSpacing(8)
        bico = QLabel()
        bico.setPixmap(icon_pixmap("merge", T["ACCENT"], 18))
        bico.setFixedWidth(22)
        bbr.addWidget(bico)
        self.lbl_branch = QLabel("")
        self.lbl_branch.setTextFormat(Qt.RichText)
        self.lbl_branch.setWordWrap(True)
        self.lbl_branch.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bbr.addWidget(self.lbl_branch, 1)
        self.btn_branch_sync = QPushButton("Aggiorna dal principale")
        self.btn_branch_sync.setToolTip("Porta nel branch le novità del principale (richiesta di merge dentro il branch)")
        self.btn_branch_merge = QPushButton("Unisci nel principale...")
        self.btn_branch_merge.setProperty("variant", "primary")
        self.btn_branch_merge.setToolTip("Propone al principale le modifiche del branch (richiesta di merge con consenso)")
        self.btn_branch_leave = QPushButton("Torna al principale")
        self.btn_branch_leave.setProperty("variant", "ghost")
        for b in (self.btn_branch_sync, self.btn_branch_merge, self.btn_branch_leave):
            bbr.addWidget(b)
        main.addWidget(self.branch_bar)
        self.branch_bar.setVisible(False)
        main.addWidget(self.lbl_master)
        main.addWidget(self.lbl_local)

        self.folder_bar = QFrame()
        self.folder_bar.setObjectName("InfoBar")
        frow = QHBoxLayout(self.folder_bar)
        frow.setContentsMargins(14, 8, 8, 8)
        frow.setSpacing(8)
        fico = QLabel()
        fico.setPixmap(icon_pixmap("folder", T["MUTED"], 18))
        fico.setFixedWidth(22)
        frow.addWidget(fico)
        self.lbl_folder = QLabel("")
        self.lbl_folder.setTextFormat(Qt.RichText)
        self.lbl_folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_folder.setWordWrap(True)                                              # la nota va a capo, il percorso non allarga la pagina
        self.lbl_folder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.btn_folder = QPushButton("  Cambia cartella...")
        self.btn_folder.setIcon(icon("folder", T["TEXT"], 16))
        self.btn_folder.setToolTip("Scegli dove salvare il progetto in locale: e' anche la cartella in cui "
                                   "«Richiedi Merge» cerca il tuo file (stessa estensione del Master)")
        self.btn_open_folder = QPushButton("Apri")
        self.btn_open_folder.setProperty("variant", "ghost")
        frow.addWidget(self.lbl_folder, 1)
        frow.addWidget(self.btn_folder)
        frow.addWidget(self.btn_open_folder)
        main.addWidget(self.folder_bar)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_checkout = QPushButton("  Checkout ultimo Master")
        self.btn_checkout.setIcon(icon("download", T["TEXT"], 18))
        self.btn_merge = QPushButton("  Richiedi Merge...")
        self.btn_merge.setProperty("variant", "primary")
        self.btn_merge.setIcon(icon("upload", T["TEXT_ON_ACCENT"], 18))
        self.btn_history = QPushButton("  Storico")
        self.btn_history.setIcon(icon("history", T["TEXT"], 18))
        self.btn_commit = QPushButton("  Salva su branch...")
        self.btn_commit.setProperty("variant", "primary")
        self.btn_commit.setIcon(icon("upload", T["TEXT_ON_ACCENT"], 18))
        self.btn_commit.setToolTip("Salva direttamente una nuova revisione nel tuo branch personale")
        self.btn_commit.setVisible(False)
        self.btn_branches = QPushButton("  Branch...")
        self.btn_branches.setIcon(icon("merge", T["TEXT"], 18))
        self.btn_branches.setToolTip("Elenco dei branch del progetto: crea, passa, aggiorna, unisci, chiudi")
        self.btn_import = QPushButton("  Importa file base...")
        self.btn_import.setProperty("variant", "primary")
        self.btn_import.setIcon(icon("upload", T["TEXT_ON_ACCENT"], 18))
        self.btn_import.setToolTip("Il Master non e' ancora inizializzato: carica il file base del progetto "
                                   "(L5K, L5X o qualsiasi altro file)")
        self.btn_reimport = QPushButton("  Reimporta file base...")
        self.btn_reimport.setProperty("variant", "warning")
        self.btn_reimport.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_reimport.setToolTip("Crea una nuova revisione del Master da un file base aggiornato "
                                     "(la cronologia resta intatta)")
        self.btn_refresh = QPushButton()
        self.btn_refresh.setIcon(icon("refresh", T["TEXT"], 18))
        self.btn_refresh.setToolTip("Aggiorna")
        self.btn_refresh.setProperty("variant", "ghost")
        for b in (self.btn_checkout, self.btn_merge, self.btn_commit, self.btn_history, self.btn_branches,
                  self.btn_import, self.btn_reimport):
            bar.addWidget(b)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        self.btn_active, self.btn_all = QPushButton("Attive"), QPushButton("Tutte")
        grp = QButtonGroup(self)
        for b, pos in ((self.btn_active, "left"), (self.btn_all, "right")):
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setProperty("segpos", pos)
            b.setCursor(Qt.PointingHandCursor)
            grp.addButton(b)
            bar.addWidget(b)
        self.btn_active.setChecked(True)
        main.addLayout(bar)

        # ---- barra «Presa in carico» (lock esclusivo del Master): stato + prendi / rilascia / check-in / forza
        self._lock: Optional[LockOut] = None
        self._kind: Optional[str] = None                 # tipo di file del Master (il check-in e' dei soli binari)
        self.lock_bar = QFrame()
        self.lock_bar.setObjectName("InfoBar")
        lrow = QHBoxLayout(self.lock_bar)
        lrow.setContentsMargins(14, 6, 8, 6)
        lrow.setSpacing(8)
        lico = QLabel()
        lico.setPixmap(icon_pixmap("lock", T["MUTED"], 18))
        lico.setFixedWidth(22)
        lrow.addWidget(lico)
        self.lbl_lock = QLabel("")
        self.lbl_lock.setTextFormat(Qt.RichText)
        self.lbl_lock.setWordWrap(True)
        self.lbl_lock.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lrow.addWidget(self.lbl_lock, 1)
        self.btn_lock_take = QPushButton("Prendi in carico")
        self.btn_lock_release = QPushButton("Rilascia")
        self.btn_checkin = QPushButton("  Check-in nuova revisione...")
        self.btn_checkin.setIcon(icon("upload", T["TEXT_ON_ACCENT"], 16))
        self.btn_checkin.setProperty("variant", "primary")
        self.btn_lock_force = QPushButton("Forza rilascio...")
        self.btn_lock_force.setProperty("variant", "warning")
        for b in (self.btn_lock_take, self.btn_lock_release, self.btn_checkin, self.btn_lock_force):
            lrow.addWidget(b)
        main.addWidget(self.lock_bar)
        self.lock_bar.setVisible(False)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.holder = QWidget()
        self.holder.setStyleSheet("background:transparent;")
        self.cards = QVBoxLayout(self.holder)
        self.cards.setContentsMargins(0, 4, 8, 4)
        self.cards.setSpacing(14)
        self.cards.addStretch(1)
        self.scroll.setWidget(self.holder)
        self.empty = EmptyState("inbox", "Nessuna richiesta di merge",
                                "Esegui il Checkout, modifica il file offline e premi «Richiedi Merge»: "
                                "le richieste del team compariranno qui.")
        main.addWidget(self.scroll, 1)
        root.addLayout(main, 1)

        # ---------------------------------------------------------------- colonna destra
        side = QFrame()
        side.setObjectName("Panel")
        side.setFixedWidth(288)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(18, 16, 18, 16)
        sl.setSpacing(10)
        hrow = QHBoxLayout()
        hrow.addWidget(eyebrow("Programmatori"))
        hrow.addStretch(1)
        self.lbl_online = QLabel("")
        self.lbl_online.setProperty("caption", True)
        hrow.addWidget(self.lbl_online)
        sl.addLayout(hrow)
        self.devs = QVBoxLayout()
        self.devs.setSpacing(10)
        sl.addLayout(self.devs)
        sl.addSpacing(4)
        sl.addWidget(h_line())
        sl.addSpacing(2)
        sl.addWidget(eyebrow("Eventi"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setPlaceholderText("Gli eventi del progetto (consensi, merge, nuove revisioni) compariranno qui.")
        self.log.setStyleSheet(f"font-size:12px; color:{T['TEXT_DIM']};")
        sl.addWidget(self.log, 1)
        root.addWidget(side)

        self.btn_checkout.clicked.connect(self.checkout_requested)
        self.btn_merge.clicked.connect(self.merge_requested)
        self.btn_history.clicked.connect(self.history_requested)
        self.btn_import.clicked.connect(self.import_master_requested)
        self.btn_reimport.clicked.connect(self.reimport_master_requested)
        self.btn_refresh.clicked.connect(self.refresh_requested)
        self.btn_lock_take.clicked.connect(self.lock_take_requested)
        self.btn_lock_release.clicked.connect(self.lock_release_requested)
        self.btn_lock_force.clicked.connect(self.lock_force_requested)
        self.btn_checkin.clicked.connect(self.checkin_requested)
        self.btn_branches.clicked.connect(self.branches_requested)
        self.btn_commit.clicked.connect(self.commit_requested)
        self.btn_branch_sync.clicked.connect(self.branch_sync_requested)
        self.btn_branch_merge.clicked.connect(self.branch_merge_requested)
        self.btn_branch_leave.clicked.connect(self.branch_leave_requested)
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
        self._update_branch_ui()
        self._update_master_buttons()
        self._rebuild()

    def set_enabled_project(self, on: bool) -> None:
        for b in (self.btn_checkout, self.btn_merge, self.btn_history, self.btn_refresh, self.btn_folder,
                  self.btn_open_folder, self.btn_branches):
            b.setEnabled(on)
        self._project_on = on
        self._update_master_buttons()
        self._update_branch_ui()

    # ------------------------------------------------------------------ branch
    def set_merge_allowed(self, on: bool) -> None:
        """«Richiedi Merge» consentito dal progetto (Master presente, non archiviato); il branch puo' restringere."""
        self._merge_allowed = on
        self._update_branch_ui()

    def set_branch(self, slug: Optional[str], branch: Optional[BranchOut] = None) -> None:
        """Contesto di lavoro: ``slug=None`` = principale; altrimenti il branch (``branch`` = descrizione dal server,
        se gia' nota). Mostra il banner del branch e adegua pulsanti e titoli."""
        changed = (slug or None) != self._branch_slug
        self._branch_slug, self._branch = slug or None, (branch if slug else None)
        self.title.setText(self._title_text())
        if changed and self._project_on:
            self.lbl_master.setText(f"<span style='color:{T['MUTED']}'>Caricamento...</span>")
        self._update_branch_ui()
        self._update_master_buttons()

    @property
    def branch_slug(self) -> Optional[str]:
        return self._branch_slug

    def _title_text(self) -> str:
        base = self._project_name or "Merge & Consenso"
        if not self._branch_slug:
            return base
        return f"{base}  ›  {self._branch.name if self._branch else self._branch_slug}"

    def _update_branch_ui(self) -> None:
        """Pulsanti e banner in base a ruolo, branch (tipo/stato/``can_write``) e stato del progetto."""
        slug, b, role = self._branch_slug, self._branch, self._role
        dev = role >= Role.DEVELOPER
        ab = abilities(b, role, slug) if b is not None else None
        team_or_main = not slug or (b is not None and b.kind == "team")
        self.btn_merge.setVisible(dev and team_or_main)
        self.btn_merge.setEnabled(bool(self._project_on and self._merge_allowed and (not slug or (ab and ab.request_merge))))
        self.btn_merge.setToolTip("" if not slug else
                                  "Branch di sola lettura" if not (ab and ab.request_merge) else
                                  "Invia le modifiche al branch (richiesta di merge con consenso)")
        self.btn_commit.setVisible(bool(dev and b is not None))
        self.btn_commit.setEnabled(bool(ab and ab.commit))
        self.btn_commit.setToolTip(
            ("Salva direttamente una nuova revisione nel tuo branch personale" if b is not None and b.kind == "personal" else
             "Salva nel branch di team: le tue modifiche si uniscono a quelle già presenti, senza votazione") if (ab and ab.commit)
            else "Branch di sola lettura per te (personale altrui, chiuso o unito)")
        if slug:
            self.btn_checkout.setText("  Checkout testa del branch" if dev else "  Scarica testa del branch")
        else:
            self.btn_checkout.setText("  Checkout ultimo Master" if dev else "  Scarica ultimo Master")
        self.branch_bar.setVisible(bool(slug))
        if not slug:
            return
        if b is None:
            self.lbl_branch.setText(f"<b>Branch «{slug}»</b> <span style='color:{T['MUTED']}'>— caricamento...</span>")
            self._style_branch_bar(T["ACCENT_ALT"])
        else:
            col = {"merged": T["GREEN"], "closed": T["MUTED"]}.get(b.state) or (
                T["ACCENT_ALT"] if b.kind == "personal" else T["SYN_KEYWORD"])
            note = ""
            if b.state != "open":
                note = f" · <span style='color:{T['ORANGE']}'>{state_label(b.state)}: sola lettura</span>"
            elif not b.can_write:
                note = f" · <span style='color:{T['ORANGE']}'>sola lettura per te</span>"
            self.lbl_branch.setText(
                f"<span style='color:{col}; font-weight:700'>Stai lavorando sul branch «{b.name}»</span> "
                f"({kind_label(b.kind)}) <span style='color:{T['MUTED']}'>{behind_text(b)} · proprietario {b.owner}</span>"
                f"{note}")
            self._style_branch_bar(col)
        self.btn_branch_sync.setVisible(dev)
        self.btn_branch_merge.setVisible(dev)
        self.btn_branch_sync.setEnabled(bool(ab and ab.sync))
        self.btn_branch_merge.setEnabled(bool(ab and ab.merge))
        self.btn_branch_sync.setToolTip("Porta nel branch le novità del principale (richiesta di merge dentro il branch)"
                                        if (ab and ab.sync) else
                                        "Il branch è già aggiornato rispetto al principale" if (ab and ab.merge) else
                                        "Operazione non disponibile (branch di sola lettura)")

    def _style_branch_bar(self, color: str) -> None:
        self.branch_bar.setStyleSheet(f"QFrame#BranchBar {{ background-color: {tint(color, 26)}; "
                                      f"border: 1px solid {tint(color, 110)}; border-radius: 8px; }}")

    def set_master_state(self, initialized: Optional[bool]) -> None:
        """Master inizializzato o no: decide quale tra «Importa» e «Reimporta file base» e' disponibile (solo Lead/Admin)."""
        self._master_init = initialized
        self._update_master_buttons()

    def _update_master_buttons(self) -> None:
        can = self._project_on and self._role >= Role.LEAD and self._master_init is not None and not self._branch_slug
        self.btn_import.setVisible(bool(can and not self._master_init))
        self.btn_reimport.setVisible(bool(can and self._master_init))                  # il file base si reimporta solo sul principale
        self._update_lock_bar()

    def set_master(self, project_name: str, m: Optional[MasterInfo]) -> None:
        self._project_name = project_name or ""
        self.title.setText(self._title_text())
        if m is None:
            if self._branch_slug:
                self.lbl_master.setText(f"<span style='color:{T['ORANGE']}'>Branch senza revisioni disponibili</span>")
                return
            self.lbl_master.setText(f"<span style='color:{T['ORANGE']}'>Master non ancora inizializzato</span> "
                                    "— un Lead o un Admin deve caricare il file Master (L5K, L5X o qualsiasi altro file).")
            return
        what = "Master" if not self._branch_slug else f"Branch «{self._branch.name if self._branch else self._branch_slug}»"
        self.lbl_master.setText(
            f"<span style='color:{T['ACCENT']}; font-weight:700'>{what} rev {m.revision}</span> &nbsp; "
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

    def set_presence(self, people: list[PresenceOut]) -> None:
        """Colleghi con, per ciascuno, dove lavorano (Master / branch), da quale revisione e chi tiene il lock."""
        while self.devs.count():
            w = self.devs.takeAt(0).widget()
            if w:
                drop_widget(w)
        for d in people:
            row = QWidget()
            row.setObjectName("cell")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            rl.addWidget(Avatar(d.username, 30, ring=T["GREEN"] if d.online else None))
            col = QVBoxLayout()
            col.setSpacing(0)
            n = QLabel(d.username)
            n.setStyleSheet("font-weight:600;")
            role = d.role or ""
            sub = QLabel(role.replace("_", " "))
            sub.setStyleSheet(f"color:{role_color(role) if role else T['MUTED']}; font-size:11px;")
            col.addWidget(n)
            col.addWidget(sub)
            for c in d.contexts:
                where = c.branch_name or c.branch or "Master"
                txt = f"{'● ' if c.online else '○ '}{where}" + (f" · rev {c.revision}" if c.revision else "")
                if c.holds_lock:
                    txt += " · 🔒 ha in carico il Master"
                cl = QLabel(txt)
                cl.setStyleSheet(f"color:{T['GREEN'] if c.online else T['MUTED']}; font-size:11px;")
                cl.setObjectName("presence_ctx")
                col.addWidget(cl)
            rl.addLayout(col, 1)
            self.devs.addWidget(row)
        online = sum(1 for d in people if d.online)
        self.lbl_online.setText(f"{online} online" if people else "")
        if not people:
            self.devs.addWidget(EmptyState("users", "Nessuno connesso", "", dashed=False, compact=True))

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
            rl.addWidget(Avatar(d.developer, 30, ring=T["GREEN"] if d.connected else None))
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
        online = sum(1 for d in devs if d.connected)
        self.lbl_online.setText(f"{online} online" if devs else "")
        if not devs:
            self.devs.addWidget(EmptyState("users", "Nessuno connesso", "", dashed=False, compact=True))

    def log_event(self, msg: str) -> None:
        if msg:
            self.log.appendPlainText(msg)

    # ------------------------------------------------------------------ presa in carico (lock)
    def set_lock(self, state: Optional[LockOut]) -> None:
        """Stato del lock del Master (``None`` = non noto). Aggiorna testo e pulsanti in base al ruolo e al tipo di file."""
        self._lock = state
        self._update_lock_bar()

    def set_master_kind(self, kind: Optional[str]) -> None:
        """Tipo di file del Master: «Check-in nuova revisione» e' visibile solo per i progetti binari."""
        self._kind = kind
        self._update_lock_bar()

    def lock_is_mine(self) -> bool:
        return bool(self._lock and self._lock.locked and self._lock.mine)

    def lock_holder(self) -> str:
        return self._lock.holder if (self._lock and self._lock.locked) else ""

    @staticmethod
    def _until(dt) -> str:                                                           # noqa: ANN001
        loc = dt.astimezone()
        return f"{loc:%H:%M}" if loc.date() == loc.now().date() else f"{loc:%d/%m %H:%M}"

    def _update_lock_bar(self) -> None:
        on = bool(self._project_on and self._master_init)
        self.lock_bar.setVisible(on)
        if not on:
            return
        st, dev = self._lock, self._role >= Role.DEVELOPER
        held = bool(st and st.locked)
        mine, other = held and st.mine, held and not st.mine
        binary = self._kind == "binary"
        if st is None:
            text = f"<span style='color:{T['MUTED']}'>Presa in carico: —</span>"
        elif not held:
            text = f"<span style='color:{T['GREEN']}; font-weight:700'>Libero</span>"
        else:
            note = f" &nbsp;<span style='color:{T['MUTED']}'>«{st.note}»</span>" if st.note else ""
            adv = ("" if binary or not other else
                   f" &nbsp;<span style='color:{T['ORANGE']}'>Avviso: per questo tipo di file la presa in carico è solo "
                   "consultiva, puoi comunque richiedere il merge.</span>")
            text = (f"<span style='color:{T['ORANGE']}; font-weight:700'>In carico a {st.holder} fino alle "
                    f"{self._until(st.expires_at)}</span>{' (tu)' if mine else ''}{note}{adv}")
        self.lbl_lock.setText(f"<span style='color:{T['MUTED']}'>Presa in carico:</span> {text}")
        self.btn_lock_take.setVisible(dev and not mine)
        self.btn_lock_take.setEnabled(not held)
        self.btn_lock_take.setToolTip("Segnala al team che stai lavorando sul Master" if not other else
                                      f"Già in carico a {st.holder}")
        self.btn_lock_release.setVisible(bool(mine))
        self.btn_checkin.setVisible(binary and dev)
        self.btn_checkin.setEnabled(bool(mine))
        self.btn_checkin.setToolTip("Carica la nuova versione del file (messaggio obbligatorio)" if mine else
                                    "Prima prendi in carico il Master")
        self.btn_lock_force.setVisible(bool(self._role >= Role.ADMIN and other))

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
