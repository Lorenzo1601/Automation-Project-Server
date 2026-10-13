"""Helper condivisi dalle viste."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QScrollArea, QSizePolicy,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QWidget)

from client.theme.tokens import FONT_MONO, FONT_UI, T
from client.theme.widgets import role_color
from common.roles import ROLE_LABELS, Role


def human_time(dt: Optional[datetime]) -> str:
    """'12:31 · 5 min fa' (ora locale)."""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    loc = dt.astimezone()
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 45:
        rel = "adesso"
    elif secs < 3600:
        rel = f"{int(secs // 60) or 1} min fa"
    elif secs < 86400:
        rel = f"{int(secs // 3600)} h fa"
    else:
        rel = f"{int(secs // 86400)} g fa"
    return f"{loc:%H:%M} · {rel}" if secs < 86400 else f"{loc:%d/%m/%Y %H:%M}"


def full_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone():%d/%m/%Y %H:%M:%S}"


def role_label(key: str) -> str:
    try:
        return ROLE_LABELS[Role.parse(key)]
    except ValueError:
        return key


def tint(color: str, alpha: int) -> str:
    """'#RRGGBB' -> 'rgba(r, g, b, alpha)' (alpha 0-255) per fondi e bordi trasparenti nei QSS inline."""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def chip(text: str, color: Optional[str] = None, mono: bool = False, max_chars: int = 0) -> QFrame:
    """Pillola informativa. Con ``color`` il fondo e il bordo prendono una tinta trasparente dello stesso colore."""
    f = QFrame()
    f.setObjectName("Chip")
    l = QHBoxLayout(f)
    l.setContentsMargins(9, 2, 9, 2)
    shown = text if not max_chars or len(text) <= max_chars else text[:max_chars - 1] + "…"
    lab = QLabel(shown)
    if shown != text:
        f.setToolTip(text)
    fam = FONT_MONO if mono else FONT_UI
    if color:
        f.setStyleSheet(f"QFrame#Chip {{ background-color: {tint(color, 26)}; border: 1px solid {tint(color, 78)}; "
                        f"border-radius: 10px; }}")
        lab.setStyleSheet(f"color:{color}; font-size:11px; font-weight:600; font-family:{fam}; background:transparent;")
    else:
        lab.setStyleSheet(f"font-family:{fam}; font-size:11px; color:{T['TEXT_DIM']}; background:transparent;")
    l.addWidget(lab)
    return f


_UNIT = [(re.compile(r"^Program\[(.+?)\]/Routine\[(.+?)\]$"), r"\1 › \2"),
         (re.compile(r"^Program\[(.+?)\]/Tag\[(.+?)\]$"), r"\1 › tag \2"),
         (re.compile(r"^DataType\[(.+?)\]$"), r"UDT \1"),
         (re.compile(r"^AddOnInstructionDefinition\[(.+?)\]$"), r"AOI \1"),
         (re.compile(r"^Tag\[(.+?)\]$"), r"tag \1")]


def short_unit(path: str) -> str:
    """'Program[Main]/Routine[Diagnostica]' -> 'Main › Diagnostica' (etichette brevi per i chip)."""
    for rx, rep in _UNIT:
        if rx.match(path):
            return rx.sub(rep, path)
    return path


def drop_widget(w: QWidget) -> None:
    """Rimuove subito un widget dalla vista (deleteLater da solo lo lascia dipinto fino al prossimo ciclo)."""
    w.hide()
    w.setParent(None)
    w.deleteLater()


def role_chip(key: str) -> QFrame:
    return chip(role_label(key), role_color(key))


def make_table(headers: list[str], stretch_col: Optional[int] = None) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.setShowGrid(False)
    t.verticalHeader().setDefaultSectionSize(40)
    t.horizontalHeader().setHighlightSections(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    t.horizontalHeader().setMinimumSectionSize(60)
    t.horizontalHeader().setStretchLastSection(stretch_col is None)
    if stretch_col is not None:
        t.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return t


def item(text: str, color: Optional[str] = None, data=None, bold: bool = False) -> QTableWidgetItem:       # noqa: ANN001
    it = QTableWidgetItem(text)
    if color:
        from PySide6.QtGui import QBrush, QColor
        it.setForeground(QBrush(QColor(color)))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    if data is not None:
        it.setData(Qt.UserRole, data)
    return it


def heading(text: str, level: int = 1) -> QLabel:
    l = QLabel(text)
    l.setProperty("h1" if level == 1 else "h2", True)
    return l


def muted(text: str) -> QLabel:
    l = QLabel(text)
    l.setProperty("muted", True)
    l.setWordWrap(True)
    return l


def subtitle(text: str) -> QLabel:
    """Sottotitolo di pagina (grigio, a capo automatico)."""
    l = QLabel(text)
    l.setProperty("subtitle", True)
    l.setWordWrap(True)
    return l


def eyebrow(text: str) -> QLabel:
    """Etichetta maiuscola piccola con spaziatura tra le lettere (titoletti di sezione, etichette dei campi)."""
    l = QLabel(text.upper())
    l.setProperty("eyebrow", True)
    f = QFont(l.font())
    f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
    f.setBold(True)
    l.setFont(f)
    return l


def h_line() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    return f


def polish(w: QWidget) -> None:
    """Riapplica lo stile dopo il cambio di una dynamic property."""
    w.style().unpolish(w)
    w.style().polish(w)


class ScrollStack(QStackedWidget):
    """``QStackedWidget`` le cui pagine stanno in un'area scorrevole: la finestra puo' essere ridimensionata a piacere
    (anche piu' piccola del contenuto, che si scorre) e non viene piu' tagliata dallo schermo.
    ``currentWidget()`` e ``widget(i)`` restituiscono la pagina vera, non il contenitore."""

    MIN_W, MIN_H = 480, 320

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pages: list[QWidget] = []

    def addWidget(self, w: QWidget) -> int:                                  # noqa: N802
        sa = QScrollArea()
        sa.setObjectName("pageScroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setStyleSheet("QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget { background: transparent; }")
        sa.setWidget(w)
        sa.setMinimumSize(self.MIN_W, self.MIN_H)
        sa.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pages.append(w)
        return super().addWidget(sa)

    def currentWidget(self) -> Optional[QWidget]:                            # noqa: N802
        i = self.currentIndex()
        return self._pages[i] if 0 <= i < len(self._pages) else None

    def widget(self, i: int) -> Optional[QWidget]:
        return self._pages[i] if 0 <= i < len(self._pages) else None

    def setCurrentWidget(self, w: QWidget) -> None:                          # noqa: N802
        if w in self._pages:
            self.setCurrentIndex(self._pages.index(w))

    def minimumSizeHint(self):                                               # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(self.MIN_W, self.MIN_H)


def fit_to_screen(win: QWidget, w: int, h: int) -> None:
    """Dimensione iniziale ``w`` x ``h`` ridotta, se serve, allo spazio utile dello schermo (barra delle applicazioni esclusa)."""
    from PySide6.QtGui import QGuiApplication
    scr = QGuiApplication.primaryScreen()
    if scr is not None:
        a = scr.availableGeometry()
        w, h = min(w, max(640, a.width() - 40)), min(h, max(480, a.height() - 40))
    win.resize(w, h)
