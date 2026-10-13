"""Sidebar di navigazione: progetto, sezioni, stato server in tempo reale (LED), utente."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from client.branch_info import MAIN_LABEL, behind_text, branch_choices, kind_label, state_label, summary_text
from client.theme.tokens import T
from client.theme.widgets import Avatar, StatusLed, icon, logo_pixmap, role_color
from client.views.common import eyebrow, role_label
from common.schemas import ProjectOut, UserOut
from common.schemas_branches import BranchOut


CLIENT_NAV = (("dashboard", "dashboard", "Merge & Consenso"), ("audit", "audit", "Cronologia eventi"),
              ("users", "users", "Utenti & Permessi"), ("help", "help", "Guida"))

# titoletto di gruppo mostrato sopra la voce con quella chiave (valido per client e server: le chiavi non coincidono)
NAV_GROUPS = {"dashboard": "Lavoro", "help": "Supporto", "status": "Server", "settings": "Sistema"}
# scorciatoia mostrata a destra della voce
NAV_HINTS = {"help": "F1"}


class NavButton(QToolButton):
    """Voce di menu: icona e testo allineati sempre a sinistra (un QToolButton centra il gruppo icona+testo, per cui
    voci con testo di lunghezza diversa risultano sfalsate)."""

    ICON_SIZE = 20

    def __init__(self, ico: str, text: str, parent: Optional[QWidget] = None, hint: str = ""):
        super().__init__(parent)
        self._ico, self._text = ico, text
        self.setCheckable(True)
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(17, 0, 14, 0)          # 3 px del bordo attivo + 14 px di padding, uguali per tutte le voci
        row.setSpacing(12)
        self._icon = QLabel()
        self._icon.setFixedSize(self.ICON_SIZE, self.ICON_SIZE)
        self._label = QLabel(text)
        self._label.setObjectName("NavText")
        self._label.setTextFormat(Qt.PlainText)
        for w in (self._icon, self._label):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(self._icon)
        row.addWidget(self._label, 1)
        self._hint = QLabel(hint)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.setStyleSheet(f"color:{T['MUTED']}; font-size:10px; font-weight:700; background:transparent; "
                                 f"border:1px solid {T['BORDER_STRONG']}; border-radius:4px; padding:0 5px;")
        self._hint.setVisible(bool(hint))
        row.addWidget(self._hint, 0, Qt.AlignVCenter)
        self._hover = False
        self.toggled.connect(lambda _on: self._refresh())
        self._refresh()

    def paintEvent(self, _ev) -> None:                 # noqa: ANN001, N802
        """Sfondo soffuso + barra accent per la voce attiva, velo chiaro all'hover (le etichette sono figli)."""
        if not (self.isChecked() or self._hover):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.isChecked():
            p.setBrush(QColor(0, 229, 255, 30))
            p.drawRoundedRect(QRectF(8, 2, self.width() - 16, self.height() - 4), 9, 9)
            p.setBrush(QColor(T["ACCENT"]))
            p.drawRoundedRect(QRectF(0, 9, 4, self.height() - 18), 2, 2)
        else:
            p.setBrush(QColor(255, 255, 255, 12))
            p.drawRoundedRect(QRectF(8, 2, self.width() - 16, self.height() - 4), 9, 9)

    def enterEvent(self, ev) -> None:                  # noqa: N802
        self._hover = True
        self._refresh()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:                  # noqa: N802
        self._hover = False
        self._refresh()
        super().leaveEvent(ev)

    def _refresh(self) -> None:
        color = T["ACCENT"] if self.isChecked() else (T["TEXT"] if self._hover else T["MUTED"])
        self._label.setStyleSheet(f"color:{color}; font-weight:{'600' if self.isChecked() else '500'}; background:transparent;")
        self._refresh_icon()
        self.update()

    def sizeHint(self) -> QSize:                       # noqa: D102
        return QSize(200, 42)

    def text(self) -> str:                             # noqa: D102 - il testo e' disegnato dall'etichetta interna
        return self._text

    def _refresh_icon(self) -> None:
        pm = icon(self._ico, T["ACCENT"] if self.isChecked() else T["MUTED"], self.ICON_SIZE).pixmap(
            QSize(self.ICON_SIZE, self.ICON_SIZE))
        self._icon.setPixmap(pm)


class Sidebar(QFrame):
    """Barra laterale. Per il client usa le voci di default; la GUI del server passa le proprie (``nav``)."""
    nav_changed = Signal(str)            # chiave della voce selezionata
    project_changed = Signal(int)
    branch_changed = Signal(str)         # slug del branch scelto nel selettore ("" = principale)
    new_project_requested = Signal()     # pulsante «+» accanto al progetto (visibile solo all'Admin)
    logout_requested = Signal()
    password_requested = Signal()
    notifications_requested = Signal()   # campanella: apre la posta in arrivo

    def __init__(self, parent: Optional[QWidget] = None, nav=CLIENT_NAV, subtitle: str = "Project Server",
                 show_projects: bool = True, admin_only: tuple = ("users",), logout_tip: str = "Esci",
                 show_password: bool = True):
        super().__init__(parent)
        self.admin_only = admin_only
        self.setObjectName("Sidebar")
        self.setFixedWidth(248)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 20, 0, 14)
        lay.setSpacing(0)

        # ---- marchio: logo + nome
        brand = QHBoxLayout()
        brand.setContentsMargins(18, 0, 18, 0)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(36))
        names = QVBoxLayout()
        names.setSpacing(0)
        b1, b2 = QLabel("AUTOMATION"), QLabel(subtitle)
        b1.setObjectName("Brand")
        b2.setObjectName("BrandSub")
        names.addWidget(b1)
        names.addWidget(b2)
        brand.addWidget(logo)
        brand.addSpacing(10)
        brand.addLayout(names)
        brand.addStretch(1)
        lay.addLayout(brand)
        lay.addSpacing(20)

        # ---- progetto corrente
        self._project_widgets: list = []
        pl = eyebrow("Progetto")
        pl.setContentsMargins(18, 0, 0, 0)
        self._project_widgets.append(pl)
        lay.addWidget(pl)
        self.project = QComboBox()
        self.project.setContentsMargins(0, 0, 0, 0)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(14, 6, 14, 0)
        wrap.setSpacing(6)
        wrap.addWidget(self.project, 1)
        self.btn_new_project = QToolButton()
        self.btn_new_project.setObjectName("IconBtn")
        self.btn_new_project.setIcon(icon("plus", T["ACCENT"], 18))
        self.btn_new_project.setToolTip("Nuovo progetto...")
        self.btn_new_project.setCursor(Qt.PointingHandCursor)
        self.btn_new_project.setVisible(False)
        self.btn_new_project.clicked.connect(self.new_project_requested)
        wrap.addWidget(self.btn_new_project)
        lay.addLayout(wrap)
        self._project_widgets.append(self.project)
        self.project.activated.connect(lambda _i: self.project_changed.emit(self.project.currentData()))

        # ---- branch corrente: «Principale» oppure uno dei branch aperti del progetto
        lay.addSpacing(12)
        bl = eyebrow("Branch")
        bl.setContentsMargins(18, 0, 0, 0)
        self._project_widgets.append(bl)
        lay.addWidget(bl)
        self.branch = QComboBox()
        self.branch.setObjectName("BranchCombo")
        self.branch.setToolTip("Linea di lavoro: il principale oppure un branch del progetto")
        bwrap = QHBoxLayout()
        bwrap.setContentsMargins(14, 6, 14, 0)
        bwrap.addWidget(self.branch, 1)
        lay.addLayout(bwrap)
        self.lbl_branch = QLabel("")
        self.lbl_branch.setTextFormat(Qt.RichText)
        self.lbl_branch.setWordWrap(True)
        self.lbl_branch.setStyleSheet(f"color:{T['MUTED']}; font-size:11px;")
        self.lbl_branch.setContentsMargins(18, 4, 14, 0)
        lay.addWidget(self.lbl_branch)
        self._project_widgets.extend([self.branch, self.lbl_branch])
        self._branches: list[BranchOut] = []
        self.branch.activated.connect(lambda _i: self.branch_changed.emit(self.branch.currentData() or ""))
        self.set_branches([], None)
        lay.addSpacing(18)
        for w_ in self._project_widgets:
            w_.setVisible(show_projects)

        # ---- navigazione (voci a tutta larghezza, titoletti di gruppo)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, NavButton] = {}
        self.group_labels: dict[str, QLabel] = {}
        for key, ico, text in nav:
            if key in NAV_GROUPS:
                gl = eyebrow(NAV_GROUPS[key])
                gl.setContentsMargins(18, 0, 0, 0)
                lay.addSpacing(8 if self.group_labels else 0)
                lay.addWidget(gl)
                lay.addSpacing(4)
                self.group_labels[key] = gl
            b = NavButton(ico, text, hint=NAV_HINTS.get(key, ""))
            b.clicked.connect(lambda _c, k=key: self.nav_changed.emit(k))
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
        self.buttons[nav[0][0]].setChecked(True)
        lay.addStretch(1)

        # ---- stato server (indicatore di connessione)
        self.conn = QFrame()
        self.conn.setObjectName("ConnChip")
        st = QHBoxLayout(self.conn)
        st.setContentsMargins(6, 6, 12, 6)
        st.setSpacing(2)
        self.led = StatusLed(diameter=8)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.lbl_state = QLabel("Offline")
        self.lbl_state.setStyleSheet("font-weight:600;")
        self.lbl_detail = QLabel("")
        self.lbl_detail.setObjectName("BrandSub")
        col.addWidget(self.lbl_state)
        col.addWidget(self.lbl_detail)
        st.addWidget(self.led)
        st.addLayout(col, 1)
        self.btn_bell = QToolButton()
        self.btn_bell.setObjectName("IconBtn")
        self.btn_bell.setIcon(icon("bell", T["MUTED"], 18))
        self.btn_bell.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_bell.setToolTip("Notifiche")
        self.btn_bell.setCursor(Qt.PointingHandCursor)
        self.btn_bell.clicked.connect(lambda _c=False: self.notifications_requested.emit())
        self.btn_bell.setVisible(show_projects)        # la console del server non ha posta in arrivo
        st.addWidget(self.btn_bell)
        cw = QHBoxLayout()
        cw.setContentsMargins(12, 0, 12, 0)
        cw.addWidget(self.conn)
        lay.addLayout(cw)
        lay.addSpacing(8)

        # ---- utente
        self.chip = QFrame()
        self.chip.setObjectName("UserChip")
        cl = QHBoxLayout(self.chip)
        cl.setContentsMargins(10, 8, 6, 8)
        cl.setSpacing(8)
        self.avatar_box = QHBoxLayout()
        self.avatar_box.setContentsMargins(0, 0, 0, 0)
        cl.addLayout(self.avatar_box)
        col2 = QVBoxLayout()
        col2.setSpacing(0)
        self.lbl_user, self.lbl_role = QLabel("-"), QLabel("")
        self.lbl_user.setStyleSheet("font-weight:600;")
        self.lbl_role.setStyleSheet(f"font-size:11px; color:{T['MUTED']};")
        col2.addWidget(self.lbl_user)
        col2.addWidget(self.lbl_role)
        cl.addLayout(col2, 1)
        self.btn_pw = QToolButton()
        self.btn_pw.setObjectName("IconBtn")
        self.btn_pw.setIcon(icon("key", T["MUTED"], 18))
        self.btn_pw.setToolTip("Cambia password")
        self.btn_pw.setCursor(Qt.PointingHandCursor)
        self.btn_pw.clicked.connect(self.password_requested)
        self.btn_out = QToolButton()
        self.btn_out.setObjectName("IconBtn")
        self.btn_out.setIcon(icon("logout", T["MUTED"], 18))
        self.btn_out.setToolTip(logout_tip)
        self.btn_out.setCursor(Qt.PointingHandCursor)
        self.btn_out.clicked.connect(self.logout_requested)
        if show_password:
            cl.addWidget(self.btn_pw)
        else:
            self.btn_pw.hide()
        cl.addWidget(self.btn_out)
        w = QHBoxLayout()
        w.setContentsMargins(12, 0, 12, 0)
        w.addWidget(self.chip)
        lay.addLayout(w)
        self._avatar: Optional[Avatar] = None

    # ------------------------------------------------------------------ API
    def set_user(self, u: UserOut) -> None:
        if self._avatar:
            self._avatar.deleteLater()
        self._avatar = Avatar(u.username, 34)
        self.avatar_box.addWidget(self._avatar)
        shown = u.display_name or u.username
        self.lbl_user.setText(shown if len(shown) <= 15 else u.username)
        self.lbl_user.setToolTip(f"{u.display_name} ({u.username})")
        self.lbl_role.setText(u.role.replace("_", " "))
        self.lbl_role.setStyleSheet(f"font-size:11px; color:{role_color(u.role)};")
        for k in self.admin_only:
            if k in self.buttons:
                self.buttons[k].setVisible(u.role == "Admin")

    def set_unread(self, n: int) -> None:
        """Numero di notifiche non lette accanto alla campanella (arancione se > 0)."""
        self.unread = n
        self.btn_bell.setIcon(icon("bell", T["ORANGE"] if n else T["MUTED"], 18))
        self.btn_bell.setText(str(n) if n else "")
        self.btn_bell.setStyleSheet(f"color:{T['ORANGE']}; font-weight:700;" if n else "")
        self.btn_bell.setToolTip(f"{n} notifiche non lette" if n else "Notifiche")

    unread = 0

    def set_can_create_project(self, on: bool) -> None:
        self.btn_new_project.setVisible(on)

    def set_projects(self, projects: list[ProjectOut], current: Optional[int]) -> None:
        self.project.blockSignals(True)
        self.project.clear()
        for p in projects:
            self.project.addItem(p.name + ("  (archiviato)" if p.archived else ""), p.id)
        if current is not None:
            i = self.project.findData(current)
            if i >= 0:
                self.project.setCurrentIndex(i)
        self.project.blockSignals(False)

    def set_branches(self, branches: list[BranchOut], current: Optional[str]) -> None:
        """Riempie il selettore: «Principale» + branch aperti (+ il corrente anche se chiuso/unito). ``current`` = slug
        selezionato (None = principale). Non emette ``branch_changed``."""
        self._branches = list(branches)
        self.branch.blockSignals(True)
        self.branch.clear()
        self.branch.addItem(MAIN_LABEL, "")
        self.branch.setItemData(0, "Linea principale del progetto", Qt.ToolTipRole)
        for b in branch_choices(branches, current):
            tail = "" if b.state == "open" else f" ({state_label(b.state)})"
            self.branch.addItem(f"{b.name}  ·  {kind_label(b.kind)}{tail}", b.slug)
            self.branch.setItemData(self.branch.count() - 1, f"{b.name}: {summary_text(b)} · proprietario {b.owner}",
                                    Qt.ToolTipRole)
        self.select_branch(current)
        self.branch.blockSignals(False)

    def select_branch(self, slug: Optional[str]) -> None:
        """Mostra come corrente il branch ``slug`` (None = principale) e aggiorna la didascalia."""
        i = self.branch.findData(slug or "")
        was = self.branch.blockSignals(True)
        self.branch.setCurrentIndex(i if i >= 0 else 0)
        self.branch.blockSignals(was)
        b = next((x for x in self._branches if x.slug == slug), None) if slug else None
        if b is not None:
            col = T["ACCENT_ALT"] if b.kind == "personal" else T["SYN_KEYWORD"]
            self.lbl_branch.setText(f"<span style='color:{col}; font-weight:700'>{kind_label(b.kind).capitalize()}</span>"
                                    f" · {behind_text(b)}")
        elif slug:
            self.lbl_branch.setText(f"<span style='color:{T['ORANGE']}'>Branch «{slug}»</span>")
        else:
            self.lbl_branch.setText("Linea principale")

    def set_connection(self, state: str, detail: str = "") -> None:
        self.led.set_state(state)
        self.lbl_state.setText({"online": "Server connesso", "connecting": "Riconnessione...",
                                "offline": "Server offline"}[state])
        self.lbl_detail.setText(detail)
        self.lbl_detail.setVisible(bool(detail))

    def select(self, key: str) -> None:
        self.buttons[key].setChecked(True)
