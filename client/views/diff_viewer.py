"""Visual diff viewer per Ladder Logic (ASCII dei Rung), Structured Text e XML (Tag / UDT).

* ``DiffViewer``   : confronto old -> new, side-by-side (con scroll sincronizzato) oppure inline stile GitHub,
                     evidenziazione intra-riga dei token modificati.
* ``ThreeWayView`` : Base | Proposta | Master per i conflitti (modifiche evidenziate rispetto alla base).
* ``LogixHighlighter``: syntax highlighting per istruzioni Logix (XIC, TON, OTE, BST/NXB/BND ...), ST e XML.

Le funzioni ``side_by_side_rows`` / ``inline_rows`` / ``rows_vs_base`` sono pure (testabili senza GUI).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextCursor,
                           QTextFormat)
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter,
                               QTextEdit, QVBoxLayout, QWidget)

from client.theme import mono_font
from client.theme.tokens import T


# =========================================================================== modello righe (puro)
@dataclass
class Row:
    text: str = ""
    kind: str = "equal"                       # equal | add | del | chg | blank
    lineno: str = ""
    spans: list[tuple[int, int]] = field(default_factory=list)   # intervalli modificati (intra-riga)

    @property
    def marker(self) -> str:
        return {"add": "+", "del": "-", "chg": "~"}.get(self.kind, " ")


def _lines(s: str) -> list[str]:
    return s.splitlines() if s else []


_TOKEN = re.compile(r"\s+|\w+|[^\w\s]")


def intraline_spans(a: str, b: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Intervalli di caratteri che differiscono tra due righe, calcolati a livello di token."""
    ta, tb = _TOKEN.findall(a), _TOKEN.findall(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    offs = lambda toks: [sum(len(t) for t in toks[:i]) for i in range(len(toks) + 1)]    # noqa: E731
    oa, ob = offs(ta), offs(tb)
    sa: list[tuple[int, int]] = []
    sb: list[tuple[int, int]] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if i2 > i1:
            sa.append((oa[i1], oa[i2]))
        if j2 > j1:
            sb.append((ob[j1], ob[j2]))
    return sa, sb


def side_by_side_rows(old: str, new: str) -> tuple[list[Row], list[Row]]:
    """Righe allineate: i due lati hanno sempre lo stesso numero di righe (riempimento con ``blank``)."""
    a, b = _lines(old), _lines(new)
    left: list[Row] = []
    right: list[Row] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                left.append(Row(a[i1 + k], "equal", str(i1 + k + 1)))
                right.append(Row(b[j1 + k], "equal", str(j1 + k + 1)))
            continue
        n, m = i2 - i1, j2 - j1
        for k in range(max(n, m)):
            if k < n and k < m:
                sa, sb = intraline_spans(a[i1 + k], b[j1 + k])
                left.append(Row(a[i1 + k], "chg", str(i1 + k + 1), sa))
                right.append(Row(b[j1 + k], "chg", str(j1 + k + 1), sb))
            elif k < n:
                left.append(Row(a[i1 + k], "del", str(i1 + k + 1)))
                right.append(Row("", "blank"))
            else:
                left.append(Row("", "blank"))
                right.append(Row(b[j1 + k], "add", str(j1 + k + 1)))
    return left, right


def inline_rows(old: str, new: str) -> list[Row]:
    """Diff unificato stile GitHub: righe rimosse (rosso) seguite dalle aggiunte (verde)."""
    a, b = _lines(old), _lines(new)
    rows: list[Row] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            rows += [Row(a[i1 + k], "equal", str(j1 + k + 1)) for k in range(i2 - i1)]
            continue
        n, m = i2 - i1, j2 - j1
        spans = {k: intraline_spans(a[i1 + k], b[j1 + k]) for k in range(min(n, m))}
        rows += [Row(a[i1 + k], "del", str(i1 + k + 1), spans[k][0] if k in spans else []) for k in range(n)]
        rows += [Row(b[j1 + k], "add", str(j1 + k + 1), spans[k][1] if k in spans else []) for k in range(m)]
    return rows


def rows_vs_base(base: str, other: str) -> list[Row]:
    """Mostra ``other`` evidenziando le differenze rispetto a ``base`` (pannelli dei conflitti)."""
    if not other:
        return [Row("(elemento eliminato)", "del")]
    return inline_rows(base, other) if base else [Row(t, "add", str(i + 1)) for i, t in enumerate(_lines(other))]


def diff_stats(old: str, new: str) -> tuple[int, int, int]:
    """(aggiunte, rimozioni, modifiche) in numero di righe."""
    l, r = side_by_side_rows(old, new)
    add = sum(1 for x in r if x.kind == "add")
    dele = sum(1 for x in l if x.kind == "del")
    chg = sum(1 for x in l if x.kind == "chg")
    return add, dele, chg


# =========================================================================== syntax highlighting
LOGIX_INSTRUCTIONS = (
    "XIC XIO OTE OTL OTU ONS OSR OSF OSRI OSFI TON TOF RTO CTU CTD CTUD RES MOV MVM MVMT COP CPS CLR SWPB "
    "ADD SUB MUL DIV MOD NEG ABS SQR CPT CMP EQU NEQ GRT GEQ LES LEQ LIM MEQ AND OR XOR NOT BTD BAND BOR BXOR BNOT "
    "JSR SBR RET AFI NOP MSG GSV SSV FAL FSC FFL FFU LFL LFU SQO SQI SQL MCR UID UIE JMP LBL TND EOT SIZE "
    "PID PIDE SCL SCRV ALM DEDT FGEN TOT LDL1 LDL2 HPF LPF NTCH INTG DERV SRT SOC CONCAT DTOS STOD RTOS STOR "
    "ESEL SSUM SNEG ASIN ACOS ATAN SIN COS TAN LN LOG XPY TOD FRD DEG RAD TRN"
).split()
_BRANCH = ("BST", "NXB", "BND")
_ST_KEYWORDS = ("IF THEN ELSE ELSIF END_IF FOR TO BY DO END_FOR WHILE END_WHILE CASE OF END_CASE REPEAT UNTIL "
                "END_REPEAT RETURN EXIT AND OR XOR NOT MOD TRUE FALSE").split()


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    f.setFontItalic(italic)
    return f


class LogixHighlighter(QSyntaxHighlighter):
    """Colora istruzioni Logix, branch BST/NXB/BND, membri (.DN .EN .TT ...), numeri, commenti, ST e XML."""

    def __init__(self, document) -> None:                       # noqa: ANN001
        super().__init__(document)
        R = re.compile
        self.rules: list[tuple[re.Pattern, int, QTextCharFormat]] = [
            (R(r"\b\d+(?:\.\d+)?\b"), 0, _fmt(T["SYN_NUMBER"])),
            (R(r"(?<=[\w\]])\.([A-Za-z_]\w*)"), 1, _fmt(T["SYN_MEMBER"])),
            (R(r"\b(?:" + "|".join(_ST_KEYWORDS) + r")\b"), 0, _fmt(T["SYN_KEYWORD"], bold=True)),
            (R(r"\b(?:" + "|".join(LOGIX_INSTRUCTIONS) + r")(?=\()"), 0, _fmt(T["SYN_INSTR"], bold=True)),
            (R(r"\b(?:" + "|".join(_BRANCH) + r")\b"), 0, _fmt(T["SYN_BRANCH"], bold=True)),
            (R(r"'[^']*'"), 0, _fmt(T["SYN_STRING"])),
            (R(r"</?[\w:]+"), 0, _fmt(T["SYN_XMLTAG"])),
            (R(r"/?>"), 0, _fmt(T["SYN_XMLTAG"])),
            (R(r"\b[\w:]+(?==\")"), 0, _fmt(T["SYN_MEMBER"])),
            (R(r"\"[^\"]*\""), 0, _fmt(T["SYN_STRING"])),
            (R(r"<!\[CDATA\[|\]\]>"), 0, _fmt(T["SYN_COMMENT"])),
            (R(r"//.*$"), 0, _fmt(T["SYN_COMMENT"], italic=True)),
            (R(r"\(\*.*?\*\)"), 0, _fmt(T["SYN_COMMENT"], italic=True)),
        ]

    def highlightBlock(self, text: str) -> None:
        for rx, group, fmt in self.rules:
            for m in rx.finditer(text):
                self.setFormat(m.start(group), m.end(group) - m.start(group), fmt)


# =========================================================================== CodeView
class _Gutter(QWidget):
    def __init__(self, view: "CodeView") -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self) -> QSize:
        return QSize(self.view.gutter_width(), 0)

    def paintEvent(self, e) -> None:                            # noqa: ANN001
        self.view.paint_gutter(e)


_KIND_BG = {"add": T["ADD_BG"], "del": T["DEL_BG"], "chg": T["CHG_BG"], "blank": T["FILL_BG"]}
_KIND_STRONG = {"add": T["ADD_STRONG"], "del": T["DEL_STRONG"], "chg": T["CHG_STRONG"]}
_KIND_FG = {"add": T["GREEN"], "del": T["RED"], "chg": T["ORANGE"]}


class CodeView(QPlainTextEdit):
    """Editor read-only con gutter (numeri riga + marcatore), sfondi per riga e highlight intra-riga."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("CodeView")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(mono_font(10))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self._rows: list[Row] = []
        self._gutter = _Gutter(self)
        self.highlighter = LogixHighlighter(self.document())
        self.blockCountChanged.connect(lambda _n: self._update_gutter_width())
        self.updateRequest.connect(self._scroll_gutter)
        self._update_gutter_width()

    # ---- contenuto
    def set_rows(self, rows: list[Row]) -> None:
        self._rows = rows
        self.setPlainText("\n".join(r.text for r in rows))
        sels: list[QTextEdit.ExtraSelection] = []
        for i, r in enumerate(rows):
            if r.kind == "equal":
                continue
            blk = self.document().findBlockByNumber(i)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = QTextCursor(blk)
            sel.format.setBackground(QBrush(QColor(_KIND_BG[r.kind])))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sels.append(sel)
            for a, b in r.spans:
                s2 = QTextEdit.ExtraSelection()
                c = QTextCursor(blk)
                c.setPosition(blk.position() + a)
                c.setPosition(blk.position() + min(b, len(r.text)), QTextCursor.KeepAnchor)
                s2.cursor = c
                s2.format.setBackground(QBrush(QColor(_KIND_STRONG.get(r.kind, T["CHG_STRONG"]))))
                sels.append(s2)
        self.setExtraSelections(sels)
        self._update_gutter_width()
        self._gutter.update()

    def rows(self) -> list[Row]:
        return self._rows

    # ---- gutter
    def gutter_width(self) -> int:
        digits = max(2, len(str(max([int(r.lineno) for r in self._rows if r.lineno.isdigit()] or [1]))))
        return 26 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _scroll_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, e) -> None:                           # noqa: ANN001
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event) -> None:                      # noqa: ANN001
        p = QPainter(self._gutter)
        p.fillRect(event.rect(), QColor(T["BG_1"]))
        blk = self.firstVisibleBlock()
        n = blk.blockNumber()
        top = round(self.blockBoundingGeometry(blk).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(blk).height())
        w = self._gutter.width()
        while blk.isValid() and top <= event.rect().bottom():
            if blk.isVisible() and bottom >= event.rect().top() and n < len(self._rows):
                r = self._rows[n]
                if r.kind != "equal":
                    p.fillRect(0, top, w, bottom - top, QColor(_KIND_BG[r.kind]))
                if r.kind != "blank":
                    p.setPen(QColor(_KIND_FG.get(r.kind, T["MUTED"])))
                    p.drawText(0, top, w - 6, self.fontMetrics().height(), Qt.AlignRight, f"{r.lineno}")
                    if r.marker.strip():
                        p.drawText(4, top, 12, self.fontMetrics().height(), Qt.AlignLeft, r.marker)
            blk = blk.next()
            top, bottom, n = bottom, bottom + round(self.blockBoundingRect(blk).height()), n + 1


# =========================================================================== DiffViewer
class DiffViewer(QWidget):
    """Confronto visivo old -> new con modalita' Side-by-Side / Inline."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._old = self._new = ""
        self._mode = "side"
        self.lbl_old, self.lbl_new = QLabel("Prima"), QLabel("Dopo")
        for l in (self.lbl_old, self.lbl_new):
            l.setObjectName("DiffLabel")
        self.left, self.right, self.inline = CodeView(), CodeView(), CodeView()
        self.stats = QLabel()
        self.stats.setProperty("muted", True)
        self.btn_side, self.btn_inline = QPushButton("Side by side"), QPushButton("Inline")
        grp = QButtonGroup(self)
        for b, tip, pos in ((self.btn_side, "Due colonne affiancate", "left"), (self.btn_inline, "Diff unificato", "right")):
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setProperty("segpos", pos)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            grp.addButton(b)
        self.btn_side.setChecked(True)
        self.btn_side.clicked.connect(lambda: self.set_mode("side"))
        self.btn_inline.clicked.connect(lambda: self.set_mode("inline"))

        bar = QHBoxLayout()
        bar.addWidget(self.btn_side)
        bar.addWidget(self.btn_inline)
        bar.addSpacing(12)
        bar.addWidget(self.stats)
        bar.addStretch(1)

        def col(label: QLabel, view: CodeView) -> QWidget:
            w = QWidget()
            l = QVBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(0)
            l.addWidget(label)
            l.addWidget(view)
            return w
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(col(self.lbl_old, self.left))
        self.split.addWidget(col(self.lbl_new, self.right))
        self.inline_wrap = col(QLabel("Diff"), self.inline)
        self.inline_wrap.layout().itemAt(0).widget().setObjectName("DiffLabel")
        self.inline_wrap.hide()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.split, 1)
        lay.addWidget(self.inline_wrap, 1)
        # scroll sincronizzato (righe allineate -> stessa posizione)
        self._syncing = False
        for a, b in ((self.left, self.right), (self.right, self.left)):
            a.verticalScrollBar().valueChanged.connect(lambda v, other=b: self._sync(other, v))
            a.horizontalScrollBar().valueChanged.connect(lambda v, other=b: self._sync_h(other, v))

    def _sync(self, other: CodeView, v: int) -> None:
        if not self._syncing:
            self._syncing = True
            other.verticalScrollBar().setValue(v)
            self._syncing = False

    def _sync_h(self, other: CodeView, v: int) -> None:
        if not self._syncing:
            self._syncing = True
            other.horizontalScrollBar().setValue(v)
            self._syncing = False

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.btn_side.setChecked(mode == "side")
        self.btn_inline.setChecked(mode == "inline")
        self.split.setVisible(mode == "side")
        self.inline_wrap.setVisible(mode == "inline")
        self._render()

    def set_diff(self, old: str, new: str, old_label: str = "Prima", new_label: str = "Dopo") -> None:
        self._old, self._new = old, new
        self.lbl_old.setText(old_label)
        self.lbl_new.setText(new_label)
        self._render()

    def _render(self) -> None:
        add, dele, chg = diff_stats(self._old, self._new)
        self.stats.setText(f'<span style="color:{T["GREEN"]}">+{add}</span> &nbsp; '
                           f'<span style="color:{T["RED"]}">-{dele}</span> &nbsp; '
                           f'<span style="color:{T["ORANGE"]}">~{chg}</span>')
        if self._mode == "side":
            l, r = side_by_side_rows(self._old, self._new)
            self.left.set_rows(l)
            self.right.set_rows(r)
        else:
            self.inline.set_rows(inline_rows(self._old, self._new))


# =========================================================================== ThreeWayView
class ThreeWayView(QWidget):
    """Base | Proposta | Master per un conflitto."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.labels = [QLabel("Base"), QLabel("Proposta"), QLabel("Master")]
        self.views = [CodeView(), CodeView(), CodeView()]
        split = QSplitter(Qt.Horizontal)
        for lab, v in zip(self.labels, self.views):
            lab.setObjectName("DiffLabel")
            w = QWidget()
            l = QVBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(0)
            l.addWidget(lab)
            l.addWidget(v)
            split.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)

    def set_texts(self, base: str, mine: str, theirs: str, labels: tuple[str, str, str]) -> None:
        for lab, t in zip(self.labels, labels):
            lab.setText(t)
        self.views[0].set_rows([Row(t, "equal", str(i + 1)) for i, t in enumerate(_lines(base))] or [Row("(vuoto)", "blank")])
        self.views[1].set_rows(rows_vs_base(base, mine))
        self.views[2].set_rows(rows_vs_base(base, theirs))
