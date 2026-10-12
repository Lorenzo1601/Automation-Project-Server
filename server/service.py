"""Automation Project Server come servizio di Windows.

Il servizio parte con il PC (anche senza nessun utente collegato) e viene riavviato da Windows se si ferma per un errore.
La configurazione sta in ``%ProgramData%\\AutomationProjectServer\\service.json`` (scritta da ``install``): un servizio
gira come LocalSystem e non vede le preferenze dell'utente ne' le unita' di rete mappate, quindi la cartella dati deve
essere un percorso locale (es. ``D:\\APS``).

Uso (da prompt dei comandi *come amministratore*; l'exe e' quello unico dell'applicazione, vedi ``server/launcher.py``)::

    start /wait "" AutomationProjectServer.exe install --data-dir D:\\APS [--port 8750] [--host 0.0.0.0] [--name "Linea 1"]
    start /wait "" AutomationProjectServer.exe start | stop | restart | status | remove
    start /wait "" AutomationProjectServer.exe run        # in primo piano (prova senza installare), Ctrl+C per fermare

``python -m server.service ...`` equivale all'exe. La parte Windows usa pywin32; il resto (configurazione, ``run``) funziona
ovunque.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from common.discovery import DISCOVERY_PORT
from server.auth.security import generate_password
from server.gui.controller import EmbeddedServer, check_writable, has_data
from server.service_ctl import (SERVICE_NAME, ServiceConfig, _sc, config_path, is_admin, is_windows,  # noqa: F401
                                service_status)

DISPLAY_NAME = "Automation Project Server"
DESCRIPTION = ("Server di collaborazione per progetti PLC (merge .L5X con consenso, utenti e audit). "
               "Si avvia con il PC e non richiede nessun accesso interattivo.")
FIREWALL_RULE = "Automation Project Server"


# ------------------------------------------------------------------------------------------ esecuzione
def setup_file_logging(data_dir: Path) -> logging.Handler:
    """Il servizio non ha una console: i messaggi vanno su ``<dati>/logs/server.log`` (rotazione 1 MB x 3)."""
    logs = Path(data_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(logs / "server.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.setLevel(logging.INFO)
    for n in ("uvicorn.error", "aps"):                      # "aps.*" propaga ad "aps": un solo handler evita le righe doppie
        lg = logging.getLogger(n)
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
    return h


class ServiceRunner:
    """Avvia il server (uvicorn + scoperta automatica) con la configurazione del servizio."""

    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self.server = EmbeddedServer(cfg.data_dir, cfg.host, cfg.port, name=cfg.name,
                                     discovery_port=cfg.discovery_port, with_console=False)
        self._handler: Optional[logging.Handler] = None
        self._stop = threading.Event()

    def start(self) -> bool:
        self._handler = setup_file_logging(Path(self.cfg.data_dir))
        logging.getLogger("aps.gui").info("Avvio del servizio (dati: %s)", self.cfg.data_dir)
        self.server.open()
        ok = self.server.start()
        if not ok:
            logging.getLogger("aps.gui").error("Avvio non riuscito: %s", self.server.last_error)
        return ok

    def wait(self, poll: float = 1.0) -> None:
        """Blocca finche' non si chiede l'arresto o il server cade."""
        while not self._stop.wait(poll):
            if not self.server.running:
                logging.getLogger("aps.gui").error("Il server si e' fermato: il servizio termina (verra' riavviato da Windows)")
                break

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        self.server.close()
        self.server.detach_logging()
        if self._handler:
            for n in ("uvicorn.error", "aps"):
                logging.getLogger(n).removeHandler(self._handler)
            self._handler.close()
            self._handler = None


def run_foreground(cfg: ServiceConfig) -> int:
    r = ServiceRunner(cfg)
    if not r.start():
        print(f"[ERRORE] {r.server.last_error}", file=sys.stderr)
        r.stop()
        return 1
    print(f"Server in ascolto su {', '.join(r.server.urls())}  (Ctrl+C per fermare)")
    try:
        r.wait()
    except KeyboardInterrupt:
        pass
    for _ in range(3):                                   # un secondo Ctrl+C durante l'arresto non deve interromperlo
        try:
            r.stop()
            break
        except KeyboardInterrupt:
            continue
    return 0


# ------------------------------------------------------------------------------------------ Windows
def _firewall(add: bool, cfg: Optional[ServiceConfig] = None) -> None:
    if not is_windows():
        return
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if add and cfg:
        for proto, port in (("TCP", cfg.port), ("UDP", cfg.discovery_port)):
            if port > 0:
                subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in",
                                "action=allow", f"protocol={proto}", f"localport={port}"],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def install(cfg: ServiceConfig, admin_user: str = "admin", admin_password: Optional[str] = None,
            firewall: bool = True, start: bool = True) -> str:
    """Prepara i dati (crea l'admin se la cartella e' nuova), registra il servizio e lo avvia. Ritorna un riepilogo."""
    if not is_windows():
        raise RuntimeError("L'installazione come servizio e' disponibile solo su Windows.")
    if not is_admin():
        raise PermissionError("Servono i privilegi di amministratore: apri il prompt dei comandi con 'Esegui come amministratore'.")
    data = Path(cfg.data_dir)
    if str(data).startswith("\\\\"):
        raise ValueError("Il servizio non puo' usare percorsi di rete (UNC): scegli una cartella locale, es. D:\\APS.")
    err = check_writable(data)
    if err:
        raise OSError(err)
    lines: list[str] = []
    if not has_data(data):
        pw = admin_password or generate_password()
        boot = EmbeddedServer(data, cfg.host, cfg.port, admin_user=admin_user, admin_password=pw, with_console=False)
        boot.open()
        boot.close()
        boot.detach_logging()
        lines.append(f"Creato l'amministratore '{admin_user}'" + ("" if admin_password else f" con password: {pw}  (annotala: non verra' mostrata di nuovo)"))
    import win32service
    import win32serviceutil
    if service_status() != "not_installed":
        remove(quiet=True)
    cfg.save()
    exe = sys.executable
    frozen = getattr(sys, "frozen", False)
    win32serviceutil.InstallService(
        f"{ApsService.__module__}.ApsService", SERVICE_NAME, DISPLAY_NAME, startType=win32service.SERVICE_AUTO_START,
        exeName=exe if frozen else None, exeArgs="--run-service" if frozen else None, description=DESCRIPTION)
    # riavvio automatico in caso di errore (5 s, 5 s, 30 s); il contatore si azzera dopo un giorno
    _sc("failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/30000")
    _firewall(firewall, cfg if firewall else None)
    lines.append(f"Servizio '{SERVICE_NAME}' installato (avvio automatico). Dati: {data}")
    lines.append(f"Configurazione: {config_path()}   Registro: {data / 'logs' / 'server.log'}")
    if firewall:
        lines.append(f"Firewall: aperte TCP {cfg.port} e UDP {cfg.discovery_port}")
    if start:
        _control("start")
        lines.append("Servizio avviato.")
    return "\n".join(lines)


def remove(quiet: bool = False) -> str:
    if not is_windows():
        raise RuntimeError("Disponibile solo su Windows.")
    if not is_admin():
        raise PermissionError("Servono i privilegi di amministratore.")
    import win32serviceutil
    if service_status() == "not_installed":
        return "Il servizio non e' installato."
    if service_status() in ("running", "pending"):
        _control("stop")
    win32serviceutil.RemoveService(SERVICE_NAME)
    _firewall(False)
    return "Servizio rimosso (i dati non vengono toccati)."


def _control(action: str) -> str:
    import time

    import win32serviceutil
    if action == "start":
        win32serviceutil.StartService(SERVICE_NAME)
        want = "running"
    elif action == "stop":
        try:
            win32serviceutil.StopService(SERVICE_NAME)
        except Exception as exc:                                                    # noqa: BLE001
            if "1062" not in str(exc):                                              # 1062 = gia' fermo
                raise
        want = "stopped"
    else:
        raise ValueError(action)
    for _ in range(60):
        if service_status() == want:
            return want
        time.sleep(0.5)
    return service_status()


def _win_service_class():
    """Definisce la classe del servizio solo su Windows (importa pywin32)."""
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class ApsService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self._ev = win32event.CreateEvent(None, 0, 0, None)
            self.runner: Optional[ServiceRunner] = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.runner:
                self.runner.request_stop()
            win32event.SetEvent(self._ev)

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ""))
            try:
                cfg = ServiceConfig.load()
                self.runner = ServiceRunner(cfg)
                if not self.runner.start():
                    servicemanager.LogErrorMsg(f"{SERVICE_NAME}: avvio non riuscito: {self.runner.server.last_error}")
                    self.runner.stop()
                    return                                      # esce con errore: Windows lo riavvia (azioni di ripristino)
                self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                self.runner.wait()
            except Exception as exc:                                                # noqa: BLE001
                servicemanager.LogErrorMsg(f"{SERVICE_NAME}: {exc}")
            finally:
                if self.runner:
                    self.runner.stop()
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                                  (self._svc_name_, ""))
    return ApsService


if is_windows():
    try:
        ApsService = _win_service_class()
    except ImportError:                                                             # pywin32 non installato
        ApsService = None  # type: ignore[assignment]
else:
    ApsService = None  # type: ignore[assignment]


def host_service() -> None:
    """Chiamata da SCM (l'exe viene lanciato con ``--run-service``): consegna il controllo al gestore dei servizi."""
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ApsService)
    servicemanager.StartServiceCtrlDispatcher()


# ------------------------------------------------------------------------------------------ riga di comando
def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--run-service"]:
        host_service()
        return 0
    ap = argparse.ArgumentParser(prog="AutomationProjectServerService",
                                 description="Automation Project Server come servizio di Windows")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("install", "run"):
        p = sub.add_parser(name, help="installa il servizio" if name == "install" else "esegue il server in primo piano")
        p.add_argument("--data-dir", help="cartella dei dati (percorso locale)")
        p.add_argument("--host", default=None)
        p.add_argument("--port", type=int, default=None)
        p.add_argument("--name", default=None, help="nome mostrato ai client")
        p.add_argument("--discovery-port", type=int, default=None)
        if name == "install":
            p.add_argument("--admin-user", default="admin")
            p.add_argument("--admin-password", default=None)
            p.add_argument("--no-firewall", action="store_true")
            p.add_argument("--no-start", action="store_true")
    for name in ("start", "stop", "restart", "status", "remove"):
        sub.add_parser(name)
    a = ap.parse_args(argv)

    try:
        if a.cmd in ("install", "run"):
            try:
                base = ServiceConfig.load()
            except FileNotFoundError:
                base = None
            data = a.data_dir or (base.data_dir if base else None)
            if not data:
                ap.error("--data-dir e' obbligatorio")
            cfg = ServiceConfig(
                data_dir=str(data), host=a.host or (base.host if base else "0.0.0.0"),
                port=a.port or (base.port if base else 8750), name=a.name if a.name is not None else (base.name if base else ""),
                discovery_port=a.discovery_port if a.discovery_port is not None else (base.discovery_port if base else DISCOVERY_PORT))
            if a.cmd == "run":
                return run_foreground(cfg)
            print(install(cfg, a.admin_user, a.admin_password, firewall=not a.no_firewall, start=not a.no_start))
        elif a.cmd == "remove":
            print(remove())
        elif a.cmd in ("start", "stop"):
            if not is_windows() or not is_admin():
                raise PermissionError("Servono Windows e i privilegi di amministratore.")
            print(f"Servizio: {_control(a.cmd)}")
        elif a.cmd == "restart":
            _control("stop")
            print(f"Servizio: {_control('start')}")
        elif a.cmd == "status":
            print({"running": "In esecuzione", "stopped": "Fermo", "pending": "In transizione",
                   "not_installed": "Non installato", "unavailable": "Non disponibile (serve Windows)"}[service_status()])
    except (PermissionError, ValueError, OSError, RuntimeError) as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
