"""Entry point unico per PyInstaller (AutomationProjectServer.exe): GUI, servizio Windows e riga di comando.

In sviluppo: python run_server.py            (GUI)
             python run_server.py serve ...  (server da riga di comando, come python -m server)
"""
import sys

from server.launcher import main

if __name__ == "__main__":
    sys.exit(main())
