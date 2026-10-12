"""Punto di ingresso unico dell'exe del server (``AutomationProjectServer.exe``).

* senza argomenti            -> applicazione grafica
* ``install|start|stop|restart|status|remove|run`` -> gestione del servizio Windows (vedi ``server/service.py``)
* ``serve [opzioni]``        -> server da riga di comando senza interfaccia (``--data-dir``, ``--seed-project``, ...)
* ``--run-service``          -> uso interno: lanciato da Windows quando parte il servizio

L'exe e' "windowed" (nessuna finestra nera al doppio clic): per i comandi da terminale si collega alla console di chi lo
lancia; se non ce n'e' una (UAC, servizio) scrive l'esito in ``last_command_log()``.
Da cmd usare ``start /wait "" AutomationProjectServer.exe status`` per aspettarne la fine (cmd non attende le app grafiche).
"""
from __future__ import annotations

import sys
from typing import Optional

SERVICE_CMDS = {"install", "start", "stop", "restart", "status", "remove", "run"}

USAGE = """Automation Project Server

  AutomationProjectServer.exe                    applicazione grafica
  AutomationProjectServer.exe install --data-dir D:\\APS [--port 8750] [--host 0.0.0.0] [--name "Linea 1"]
                                                 installa il servizio Windows (da prompt come amministratore)
  AutomationProjectServer.exe start|stop|restart|status|remove
  AutomationProjectServer.exe run --data-dir D:\\APS       esegue il server in primo piano (prova)
  AutomationProjectServer.exe serve [--data-dir D:\\APS] [--port 8750] [--seed-project NOME --master FILE]
                                                 server da riga di comando, senza interfaccia

Da cmd: start /wait "" AutomationProjectServer.exe status
"""


def attach_console_or_log() -> None:
    """Rende utilizzabili print/argparse in un exe senza console: console del chiamante oppure file di log."""
    if sys.stdout is not None and sys.stderr is not None:
        return                                                   # gia' in un terminale (o in sviluppo)
    if sys.platform.startswith("win"):
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):             # ATTACH_PARENT_PROCESS
            try:
                out = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                sys.stdout = sys.stderr = out
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
                return
            except OSError:
                pass
    from server.service_ctl import last_command_log
    f = open(last_command_log(), "w", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = f


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    first = args[0] if args else ""
    if not args or first == "gui":
        from server.gui.app import main as gui_main
        gui_main()
        return 0
    if first == "--run-service":
        from server.service import host_service
        host_service()
        return 0
    attach_console_or_log()
    if first in SERVICE_CMDS:
        from server.service import main as service_main
        return service_main(args)
    if first == "serve":
        from server.__main__ import main as serve_main
        sys.argv = [sys.argv[0], *args[1:]]
        serve_main()
        return 0
    print(USAGE)
    return 0 if first in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
