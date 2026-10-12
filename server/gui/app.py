"""Avvio dell'applicazione grafica del server."""
from __future__ import annotations

import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from client.theme import apply_dark_theme
    from client.theme.widgets import app_icon
    from server.gui.main_window import ServerWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Automation Project Server")
    app.setWindowIcon(app_icon())
    apply_dark_theme(app)
    win = ServerWindow()
    if not win.start():
        sys.exit(0)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
