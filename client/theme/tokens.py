"""Design tokens del tema scuro industriale (palette VS Code / TIA Portal dark).

Unica fonte di verita' dei colori: ``dark.qss`` usa i segnaposto ``@NOME@`` sostituiti da ``build_stylesheet``;
i widget custom (LED, badge, diff viewer) leggono gli stessi valori da qui.
"""
from __future__ import annotations

TOKENS: dict[str, str] = {
    # sfondi
    "BG_0": "#121417",          # sfondo applicazione
    "BG_1": "#1B1E23",          # sidebar, barre
    # superfici (schede, pannelli)
    "SURFACE_1": "#242830",
    "SURFACE_2": "#2D323E",
    "SURFACE_3": "#363C4A",     # hover
    "BORDER": "#353B48",
    "BORDER_STRONG": "#4A5163",
    # livelli aggiuntivi (gradienti sobri di schede, sidebar, pulsanti)
    "SURFACE_TOP": "#282C36",   # scheda: parte alta del gradiente
    "SURFACE_BOTTOM": "#22262E",  # scheda: parte bassa
    "SIDEBAR_TOP": "#1C2027",
    "SIDEBAR_BOTTOM": "#14171B",
    "BORDER_SOFT": "#2C313C",
    "FIELD_BG": "#171A1F",      # campi di input
    "ROW_ALT": "#1F232A",       # riga alternata delle tabelle
    # accento
    "ACCENT": "#00E5FF",        # Industrial Cyan
    "ACCENT_ALT": "#007ACC",    # Electric Blue
    "ACCENT_DIM": "#0B3A47",    # fondo selezione
    "ACCENT_HOVER": "#5CF0FF",
    "ACCENT_PRESS": "#00BFD6",
    "ACCENT_SOFT": "rgba(0, 229, 255, 28)",     # velo trasparente (voce attiva, focus)
    # stato & diff
    "GREEN": "#00E676",         # inserimenti / approvato
    "RED": "#FF5252",           # conflitti / eliminazioni
    "ORANGE": "#FFAB40",        # modifiche / pending
    "ADD_BG": "#12382A",
    "DEL_BG": "#4A2227",
    "CHG_BG": "#473818",
    "ADD_STRONG": "#1E7B50",
    "DEL_STRONG": "#A8404A",
    "CHG_STRONG": "#9C6B1C",
    "FILL_BG": "#1A1D22",       # riga di riempimento (lato senza controparte)
    # testo
    "TEXT": "#E0E6ED",
    "TEXT_DIM": "#B3BCCB",      # testo secondario leggibile
    "MUTED": "#8A94A6",
    "DISABLED": "#5A6376",
    "TEXT_ON_ACCENT": "#041418",
    # syntax highlighting (Logix / XML / ST)
    "SYN_INSTR": "#00E5FF",
    "SYN_BRANCH": "#FFAB40",
    "SYN_NUMBER": "#B5CEA8",
    "SYN_MEMBER": "#9CDCFE",
    "SYN_STRING": "#CE9178",
    "SYN_COMMENT": "#6A7385",
    "SYN_KEYWORD": "#C586C0",
    "SYN_XMLTAG": "#569CD6",
}

FONT_UI = '"Segoe UI", "Inter", "Roboto", "Helvetica Neue", sans-serif'
FONT_MONO = '"Cascadia Mono", "JetBrains Mono", "Consolas", "DejaVu Sans Mono", monospace'

T = TOKENS            # alias breve
