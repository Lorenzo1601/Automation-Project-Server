"""Rendering HTML (Qt rich text) dei diff: funzioni pure, testabili senza GUI."""
from __future__ import annotations

import difflib
import html

ADD, DEL, CTX, NOTE = "#d3f2da", "#ffd9d9", "#ffffff", "#eef1f5"


def _row(text: str, bg: str, mark: str = "") -> str:
    body = html.escape(text) if text else "&nbsp;"
    return (f'<p style="margin:0;white-space:pre-wrap;background-color:{bg};color:#1b1b1b;">'
            f'<span style="color:#7a7a7a;">{html.escape(mark) or "&nbsp;"} </span>{body}</p>')


def _wrap(rows: list[str]) -> str:
    return ('<div style="font-family:Consolas,\'Courier New\',monospace;font-size:10pt;'
            f'background-color:#ffffff;">{"".join(rows) or _row("(vuoto)", NOTE)}</div>')


def _lines(s: str) -> list[str]:
    return s.splitlines() if s else []


def _collapse(rows_equal: list[str], keep: int = 3) -> list[str]:
    if len(rows_equal) <= keep * 2 + 2:
        return [_row(t, CTX) for t in rows_equal]
    hidden = len(rows_equal) - keep * 2
    return ([_row(t, CTX) for t in rows_equal[:keep]] + [_row(f"... {hidden} righe invariate ...", NOTE)]
            + [_row(t, CTX) for t in rows_equal[-keep:]])


def unified_html(old: str, new: str) -> str:
    """Diff completo old -> new con righe aggiunte (verde) e rimosse (rosso)."""
    a, b = _lines(old), _lines(new)
    rows: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            rows += _collapse(a[i1:i2])
        else:
            rows += [_row(t, DEL, "-") for t in a[i1:i2]] + [_row(t, ADD, "+") for t in b[j1:j2]]
    return _wrap(rows)


def highlight_vs_base(base: str, other: str) -> str:
    """Mostra ``other`` evidenziando cio' che differisce da ``base`` (usato nei pannelli dei conflitti)."""
    a, b = _lines(base), _lines(other)
    if not other:
        return _wrap([_row("(elemento eliminato)", DEL, "-")])
    rows: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            rows += _collapse(b[j1:j2])
        else:
            if op in ("delete", "replace"):
                rows += [_row(t, DEL, "-") for t in a[i1:i2]]
            rows += [_row(t, ADD, "+") for t in b[j1:j2]]
    return _wrap(rows)


def plain_html(text: str) -> str:
    return _wrap([_row(t, CTX) for t in _lines(text)] if text else [_row("(vuoto)", NOTE)])
