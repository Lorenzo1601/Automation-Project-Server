"""Widget grafici riutilizzabili: LED di stato con pulse, badge animati, avatar, icone SVG minimali."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QLabel, QSizePolicy, QVBoxLayout, QWidget

from client.theme.tokens import T
from common.schemas import ProposalState

# --------------------------------------------------------------------------------- icone (stile Feather)
_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "history": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "merge": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "audit": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "columns": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="3" x2="12" y2="21"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    "alert": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "sliders": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "power": '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
    "server": '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "info": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
}


LOGO_PATH = Path(__file__).with_name("assets") / "logo.png"


def logo_pixmap(size: int) -> QPixmap:
    """Logo dell'applicazione a ``size`` px logici (nitido anche sugli schermi ad alta densita')."""
    pm = QPixmap(str(LOGO_PATH))
    if pm.isNull():
        return icon("merge", T["ACCENT"], size).pixmap(size, size)
    pm = pm.scaled(size * 2, size * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(2)
    return pm


def app_icon() -> QIcon:
    """Icona dell'applicazione (finestre, barra delle applicazioni, area di notifica)."""
    ic = QIcon(str(LOGO_PATH))
    return ic if not ic.isNull() else icon("merge", T["ACCENT"], 24)


def icon(name: str, color: str = T["MUTED"], size: int = 20) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    out = QIcon()
    for mode_color, mode in ((color, QIcon.Normal), (T["ACCENT"], QIcon.Active)):
        pm = QPixmap(size * 2, size * 2)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        QSvgRenderer(svg.replace(color, mode_color).encode()).render(p, QRectF(0, 0, size * 2, size * 2))
        p.end()
        pm.setDevicePixelRatio(2)
        out.addPixmap(pm, mode)
    return out


def icon_pixmap(name: str, color: str = T["MUTED"], size: int = 20) -> QPixmap:
    """Pixmap nitida (device pixel ratio 2) di un'icona: per QLabel / disegno personalizzato."""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    QSvgRenderer(svg.encode()).render(p, QRectF(0, 0, size * 2, size * 2))
    p.end()
    pm.setDevicePixelRatio(2)
    return pm


def add_shadow(w: QWidget, blur: int = 36, dy: int = 10, alpha: int = 150) -> QGraphicsDropShadowEffect:
    """Ombra morbida (usare con parsimonia: scheda di login, finestre flottanti)."""
    fx = QGraphicsDropShadowEffect(w)
    fx.setBlurRadius(blur)
    fx.setOffset(0, dy)
    fx.setColor(QColor(0, 0, 0, alpha))
    w.setGraphicsEffect(fx)
    return fx


# --------------------------------------------------------------------------------- LED di stato
class StatusLed(QWidget):
    """LED con anello pulsante. Stati: ``online`` (verde), ``connecting`` (arancio, veloce), ``offline`` (rosso)."""

    COLORS = {"online": T["GREEN"], "connecting": T["ORANGE"], "offline": T["RED"]}

    def __init__(self, parent: Optional[QWidget] = None, diameter: int = 10):
        super().__init__(parent)
        self._d, self._state, self._phase = diameter, "offline", 0.0
        self.setFixedSize(diameter * 3, diameter * 3)
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1600, loopCount=-1)
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.valueChanged.connect(self._tick)
        self.set_state("offline")

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._state = state
        self._anim.stop()
        self._anim.setDuration(900 if state == "connecting" else 1800)
        if state in ("online", "connecting"):
            self._anim.start()
        else:
            self._phase = 0.0
            self.update()

    def _tick(self, v) -> None:                                  # noqa: ANN001
        self._phase = float(v)
        self.update()

    def paintEvent(self, _e) -> None:                            # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.COLORS[self._state])
        cx = cy = self.width() / 2
        if self._state != "offline":
            ring = QColor(c)
            ring.setAlphaF(max(0.0, 0.55 * (1 - self._phase)))
            r = self._d / 2 + self._phase * self._d
            p.setBrush(ring)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.setBrush(c)
        p.drawEllipse(QRectF(cx - self._d / 2, cy - self._d / 2, self._d, self._d))


# --------------------------------------------------------------------------------- badge di stato animato
_BADGES = {
    # chiave: (testo, colore, pulse)
    "pending": ("In attesa di consensi", T["ORANGE"], True),
    "lead": ("Attende il Lead", T["ORANGE"], True),
    "conflict": ("Conflitti da risolvere", T["RED"], True),
    "approved": ("Approvata", T["GREEN"], False),
    "merged": ("Unita al Master", T["ACCENT"], False),
    "rejected": ("Rifiutata", T["RED"], False),
    "cancelled": ("Annullata", T["MUTED"], False),
    "forced": ("Merge forzato", T["ORANGE"], False),
}


def badge_color(kind: str) -> str:
    """Colore di stato di una richiesta (barra laterale delle schede, badge)."""
    return _BADGES[kind][1]


def badge_kind(state: ProposalState, needs_lead_pending: bool = False, forced: bool = False) -> str:
    if state == ProposalState.MERGED:
        return "forced" if forced else "merged"
    if state == ProposalState.AWAITING_CONSENT:
        return "lead" if needs_lead_pending else "pending"
    return {ProposalState.CONFLICT: "conflict", ProposalState.APPROVED: "approved",
            ProposalState.REJECTED: "rejected", ProposalState.CANCELLED: "cancelled"}[state]


class StatusBadge(QLabel):
    """Pillola colorata; gli stati in attesa (Pending / Conflict) respirano con un alone pulsante."""

    def __init__(self, kind: str = "pending", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._kind, self._phase = kind, 0.0
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setFixedHeight(26)
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1500, loopCount=-1)
        self._anim.valueChanged.connect(self._tick)
        self.set_kind(kind)

    @property
    def kind(self) -> str:
        return self._kind

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        text, _color, pulse = _BADGES[kind]
        self.setText(text)
        self.setFixedWidth(self.fontMetrics().horizontalAdvance(text) + 42)
        self._anim.stop()
        if pulse:
            self._anim.start()
        else:
            self._phase = 0.0
        self.update()

    def _tick(self, v) -> None:                                  # noqa: ANN001
        self._phase = float(v)
        self.update()

    def paintEvent(self, _e) -> None:                            # noqa: ANN001
        _text, color, pulse = _BADGES[self._kind]
        col = QColor(color)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        bg = QColor(col)
        bg.setAlphaF(0.16 + (0.10 * (1 - abs(2 * self._phase - 1)) if pulse else 0))
        p.setBrush(bg)
        pen = QPen(col)
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        dot = QColor(col)
        if pulse:                                                # il puntino lampeggia dolcemente
            dot.setAlphaF(0.45 + 0.55 * (1 - abs(2 * self._phase - 1)))
        p.setPen(Qt.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QRectF(11, self.height() / 2 - 3.5, 7, 7))
        p.setPen(col)
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSizeF(max(8.5, f.pointSizeF() - 0.5))
        p.setFont(f)
        p.drawText(QRectF(24, 0, self.width() - 28, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())


# --------------------------------------------------------------------------------- avatar
_AVATAR_COLORS = ["#00B8D4", "#2979FF", "#7C4DFF", "#00C853", "#FFAB40", "#FF5252", "#EC407A", "#26A69A"]


def avatar_color(name: str) -> str:
    return _AVATAR_COLORS[int(hashlib.md5(name.lower().encode()).hexdigest(), 16) % len(_AVATAR_COLORS)]


class Avatar(QWidget):
    """Cerchio con iniziali; ``ring`` opzionale (verde = consenso, arancio = in attesa, rosso = rifiuto)."""

    def __init__(self, name: str, size: int = 36, ring: Optional[str] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._name, self._size, self._ring = name, size, ring
        self.setFixedSize(size, size)
        self.setToolTip(name)

    def set_ring(self, color: Optional[str]) -> None:
        self._ring = color
        self.update()

    @staticmethod
    def initials(name: str) -> str:
        parts = [p for p in name.replace(".", " ").replace("_", " ").split() if p]
        return ("".join(p[0] for p in parts[:2]) or "?").upper()

    def paintEvent(self, _e) -> None:                            # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        inset = 2.0 if self._ring else 0.5
        r = QRectF(inset, inset, self._size - 2 * inset, self._size - 2 * inset)
        p.setPen(Qt.NoPen)
        base = QColor(avatar_color(self._name))
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, base.lighter(118))
        g.setColorAt(1.0, base.darker(112))
        p.setBrush(g)
        p.drawEllipse(r)
        if self._ring:
            pen = QPen(QColor(self._ring))
            pen.setWidthF(2.0)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(1, 1, self._size - 2, self._size - 2))
        p.setPen(QColor("#0B1014"))
        f = QFont(self.font())
        f.setBold(True)
        f.setPixelSize(int(self._size * 0.38))
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, self.initials(self._name))


def role_color(role_key: str) -> str:
    return {"Admin": T["RED"], "Lead_Engineer": T["ORANGE"], "Developer": T["ACCENT"], "Viewer": T["MUTED"]}.get(role_key, T["MUTED"])



# --------------------------------------------------------------------------------- stato vuoto
class EmptyState(QFrame):
    """Segnaposto per aree senza contenuto: icona in un cerchio, titolo e testo esplicativo."""

    def __init__(self, ico: str = "inbox", title: str = "", text: str = "", parent: Optional[QWidget] = None,
                 dashed: bool = True, compact: bool = False):
        super().__init__(parent)
        self.setObjectName("EmptyState" if dashed else "EmptyPlain")
        self._ico = ico
        lay = QVBoxLayout(self)
        pad = 16 if compact else 32
        lay.setContentsMargins(pad, pad, pad, pad)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(56, 56)
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setPixmap(icon_pixmap(ico, T["ACCENT"], 26))
        self.icon_label.setStyleSheet("background-color: rgba(0,229,255,24); border-radius: 28px;")
        self.title = QLabel(title)
        self.title.setProperty("h3", True)
        self.title.setAlignment(Qt.AlignCenter)
        self.text = QLabel(text)
        self.text.setProperty("muted", True)
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.icon_label, 0, Qt.AlignHCenter)
        lay.addSpacing(6)
        lay.addWidget(self.title)
        lay.addWidget(self.text)
        lay.addStretch(1)
        self.title.setVisible(bool(title))
        self.text.setVisible(bool(text))
        self.setMinimumHeight(150 if compact else 220)

    def set_texts(self, title: str, text: str = "") -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.title.setVisible(bool(title))
        self.text.setVisible(bool(text))


# --------------------------------------------------------------------------------- sfondo "hero" del login
class HeroPanel(QFrame):
    """Pannello di marca: gradiente scuro con motivo geometrico disegnato (schema a contatti / rami di merge)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Hero")

    def paintEvent(self, _e) -> None:                            # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, w, h)
        g.setColorAt(0.0, QColor("#1B2128"))
        g.setColorAt(0.55, QColor("#161A20"))
        g.setColorAt(1.0, QColor("#0F1317"))
        p.fillRect(self.rect(), g)
        # alone accento in alto a destra
        rg = QRadialGradient(w * 0.92, h * 0.06, max(w, h) * 0.62)
        rg.setColorAt(0.0, QColor(0, 229, 255, 46))
        rg.setColorAt(1.0, QColor(0, 229, 255, 0))
        p.fillRect(self.rect(), rg)
        rg2 = QRadialGradient(w * 0.05, h * 1.0, max(w, h) * 0.55)
        rg2.setColorAt(0.0, QColor(0, 122, 204, 40))
        rg2.setColorAt(1.0, QColor(0, 122, 204, 0))
        p.fillRect(self.rect(), rg2)
        # griglia di punti
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        step = 28
        for x in range(step // 2, w, step):
            for y in range(step // 2, h, step):
                p.drawEllipse(QRectF(x - 1, y - 1, 2, 2))
        # motivo: due "binari" verticali con pioli di logica ladder e rami che si fondono
        rail = QPen(QColor(0, 229, 255, 34))
        rail.setWidthF(2)
        p.setPen(rail)
        x1, x2 = w * 0.62, w * 0.94
        p.drawLine(int(x1), int(h * 0.30), int(x1), int(h * 0.97))
        p.drawLine(int(x2), int(h * 0.30), int(x2), int(h * 0.97))
        rung = QPen(QColor(255, 255, 255, 22))
        rung.setWidthF(1.5)
        p.setPen(rung)
        y = h * 0.38
        i = 0
        while y < h * 0.96:
            p.drawLine(int(x1), int(y), int(x2), int(y))
            # contatto (due trattini) a meta' piolo
            mid = (x1 + x2) / 2 + (-18 if i % 2 else 12)
            p.setPen(QPen(QColor(0, 229, 255, 70), 2))
            p.drawLine(int(mid - 6), int(y - 7), int(mid - 6), int(y + 7))
            p.drawLine(int(mid + 6), int(y - 7), int(mid + 6), int(y + 7))
            p.setPen(rung)
            y += 46
            i += 1
        # ramo che si fonde (curva di merge)
        path_pen = QPen(QColor(0, 229, 255, 60))
        path_pen.setWidthF(2.5)
        p.setPen(path_pen)
        p.setBrush(Qt.NoBrush)
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.moveTo(w * 0.70, h * 0.10)
        path.cubicTo(w * 0.70, h * 0.22, w * 0.82, h * 0.18, w * 0.82, h * 0.30)
        p.drawPath(path)
        path2 = QPainterPath()
        path2.moveTo(w * 0.94, h * 0.08)
        path2.cubicTo(w * 0.94, h * 0.20, w * 0.82, h * 0.18, w * 0.82, h * 0.30)
        p.drawPath(path2)
        p.setPen(Qt.NoPen)
        for cx, cy, col in ((w * 0.70, h * 0.10, QColor(255, 255, 255, 110)), (w * 0.94, h * 0.08, QColor(255, 171, 64, 160)),
                            (w * 0.82, h * 0.30, QColor(0, 229, 255, 230))):
            p.setBrush(col)
            p.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))
        halo = QColor(0, 229, 255, 40)
        p.setBrush(halo)
        p.drawEllipse(QRectF(w * 0.82 - 13, h * 0.30 - 13, 26, 26))
        p.end()
