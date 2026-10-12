"""Sidebar di navigazione: progetto, sezioni, stato server in tempo reale (LED), utente."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

from client.theme.tokens import T
from client.theme.widgets import Avatar, StatusLed, icon, logo_pixmap, role_color
from client.views.common import role_label
from common.schemas import ProjectOut, UserOut


CLIENT_NAV = (("dashboard", "dashboard", "Merge & Consenso"), ("audit", "audit", "Cronologia eventi"),
              ("users", "users", "Utenti & Permessi"))


class Sidebar(QFrame):
    """Barra laterale. Per il client usa le voci di default; la GUI del server passa le proprie (``nav``)."""
    nav_changed = Signal(str)            # chiave della voce selezionata
    project_changed = Signal(int)
    logout_requested = Signal()
    password_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None, nav=CLIENT_NAV, subtitle: str = "Project Server",
                 show_projects: bool = True, admin_only: tuple = ("users",), logout_tip: str = "Esci",
                 show_password: bool = True):
        super().__init__(parent)
        self.admin_only = admin_only
        self.setObjectName("Sidebar")
        self.setFixedWidth(248)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 18, 0, 14)
        lay.setSpacing(0)

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
        brand.addSpacing(8)
        brand.addLayout(names)
        brand.addStretch(1)
        lay.addLayout(brand)
        lay.addSpacing(18)

        self._project_widgets: list = []
        pl = QLabel("PROGETTO")
        self._project_widgets.append(pl)
        pl.setStyleSheet(f"color:{T['MUTED']}; font-size:10px; font-weight:700; letter-spacing:1px; padding-left:18px;")
        lay.addWidget(pl)
        self.project = QComboBox()
        self.project.setContentsMargins(0, 0, 0, 0)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(14, 4, 14, 0)
        wrap.addWidget(self.project)
        lay.addLayout(wrap)
        self._project_widgets.append(self.project)
        self.project.activated.connect(lambda _i: self.project_changed.emit(self.project.currentData()))
        lay.addSpacing(16)
        for w_ in self._project_widgets:
            w_.setVisible(show_projects)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for key, ico, text in nav:
            b = QToolButton()
            b.setCheckable(True)
            b.setText("  " + text.replace("&", "&&"))
            b.setIcon(icon(ico, T["MUTED"], 20))
            b.setIconSize(QSize(20, 20))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setSizePolicy(b.sizePolicy().horizontalPolicy(), b.sizePolicy().verticalPolicy())
            b.setMinimumHeight(42)
            b.setCursor(Qt.PointingHandCursor)
            b.toggled.connect(lambda on, bt=b, i=ico: bt.setIcon(icon(i, T["ACCENT"] if on else T["MUTED"], 20)))
            b.clicked.connect(lambda _c, k=key: self.nav_changed.emit(k))
            self.group.addButton(b)
            self.buttons[key] = b
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(b)
            lay.addLayout(row)
            b.setMaximumWidth(16777215)
        self.buttons[nav[0][0]].setChecked(True)
        for b in self.buttons.values():
            b.setStyleSheet("text-align:left;")
        lay.addStretch(1)

        # stato server
        st = QHBoxLayout()
        st.setContentsMargins(14, 0, 14, 0)
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
        lay.addLayout(st)
        lay.addSpacing(10)

        # utente
        self.chip = QFrame()
        self.chip.setObjectName("UserChip")
        cl = QHBoxLayout(self.chip)
        cl.setContentsMargins(10, 8, 8, 8)
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
        self.btn_pw.setIcon(icon("key", T["MUTED"], 18))
        self.btn_pw.setToolTip("Cambia password")
        self.btn_pw.clicked.connect(self.password_requested)
        self.btn_out = QToolButton()
        self.btn_out.setIcon(icon("logout", T["MUTED"], 18))
        self.btn_out.setToolTip(logout_tip)
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

    def set_connection(self, state: str, detail: str = "") -> None:
        self.led.set_state(state)
        self.lbl_state.setText({"online": "Server connesso", "connecting": "Riconnessione...",
                                "offline": "Server offline"}[state])
        self.lbl_detail.setText(detail)

    def select(self, key: str) -> None:
        self.buttons[key].setChecked(True)
