"""Scorciatoia di sviluppo: equivale a ``python run_server.py`` (applicazione grafica)."""
import sys

from server.launcher import main

if __name__ == "__main__":
    sys.exit(main(["gui"]))
