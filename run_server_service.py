"""Scorciatoia di sviluppo: ``python run_server_service.py install|start|...`` equivale a ``python run_server.py ...``."""
import sys

from server.launcher import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["--help"]))
