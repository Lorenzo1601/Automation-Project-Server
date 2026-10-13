"""Tema scuro: caricamento QSS con variabili, QPalette coerente, font."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from client.theme.tokens import FONT_MONO, FONT_UI, T, TOKENS  # noqa: F401

_QSS = Path(__file__).with_name("dark.qss")


def build_stylesheet() -> str:
    """Legge ``dark.qss`` e sostituisce ``@NOME@`` con i token (errore se manca un token)."""
    values = {**TOKENS, "FONT_UI": FONT_UI, "FONT_MONO": FONT_MONO, "ASSETS": (_QSS.parent / "assets").as_posix()}
    css = _QSS.read_text("utf-8")
    missing = {m for m in re.findall(r"@([A-Z0-9_]+)@", css) if m not in values}
    if missing:
        raise KeyError(f"Token QSS non definiti: {sorted(missing)}")
    return re.sub(r"@([A-Z0-9_]+)@", lambda m: values[m.group(1)], css)


def build_palette() -> QPalette:
    p = QPalette()
    c = QColor
    p.setColor(QPalette.Window, c(T["BG_0"]))
    p.setColor(QPalette.WindowText, c(T["TEXT"]))
    p.setColor(QPalette.Base, c(T["FIELD_BG"]))
    p.setColor(QPalette.AlternateBase, c(T["SURFACE_1"]))
    p.setColor(QPalette.Text, c(T["TEXT"]))
    p.setColor(QPalette.Button, c(T["SURFACE_2"]))
    p.setColor(QPalette.ButtonText, c(T["TEXT"]))
    p.setColor(QPalette.ToolTipBase, c(T["SURFACE_2"]))
    p.setColor(QPalette.ToolTipText, c(T["TEXT"]))
    p.setColor(QPalette.Highlight, c(T["ACCENT_ALT"]))
    p.setColor(QPalette.HighlightedText, c("#FFFFFF"))
    p.setColor(QPalette.PlaceholderText, c(T["MUTED"]))
    p.setColor(QPalette.Link, c(T["ACCENT"]))
    p.setColor(QPalette.Disabled, QPalette.Text, c(T["DISABLED"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, c(T["DISABLED"]))
    return p


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    f = QFont()
    f.setFamilies([n.strip().strip('"') for n in FONT_UI.split(",")])
    f.setPointSizeF(9.5)
    app.setFont(f)
    app.setStyleSheet(build_stylesheet())


def mono_font(size: float = 10.0) -> QFont:
    f = QFont()
    f.setFamilies([n.strip().strip('"') for n in FONT_MONO.split(",")])
    f.setStyleHint(QFont.Monospace)
    f.setPointSizeF(size)
    return f
