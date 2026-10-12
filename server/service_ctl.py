"""Controllo del servizio Windows senza pywin32 (usa ``sc``): stato, privilegi, avvio con elevazione (UAC).

Usato sia dall'exe del servizio sia dalla GUI del server, che non include pywin32.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from common.discovery import DISCOVERY_PORT

SERVICE_NAME = "AutomationProjectServer"


def config_path() -> Path:
    env = os.getenv("APS_SERVICE_CONFIG")
    if env:
        return Path(env)
    base = os.getenv("ProgramData") or str(Path.home())
    return Path(base) / "AutomationProjectServer" / "service.json"


@dataclass
class ServiceConfig:
    data_dir: str
    host: str = "0.0.0.0"
    port: int = 8750
    name: str = ""
    discovery_port: int = DISCOVERY_PORT

    def save(self, path: Optional[Path] = None) -> Path:
        p = path or config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        return p

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ServiceConfig":
        p = path or config_path()
        if not p.exists():
            raise FileNotFoundError(f"Configurazione del servizio non trovata: {p}. Esegui prima 'install'.")
        d = json.loads(p.read_text("utf-8"))
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_admin() -> bool:
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                                              # noqa: BLE001
        return False


def _sc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sc", *args], capture_output=True, text=True, errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def service_status() -> str:
    """``running`` | ``stopped`` | ``pending`` | ``not_installed`` | ``unavailable`` (non Windows). Non richiede privilegi."""
    if not is_windows():
        return "unavailable"
    try:
        r = _sc("query", SERVICE_NAME)
    except OSError:
        return "unavailable"
    out = r.stdout or ""
    if r.returncode == 1060 or "1060" in out:
        return "not_installed"
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    if "PENDING" in out:
        return "pending"
    return "unavailable" if r.returncode else "stopped"



def last_command_log() -> Path:
    """File in cui un comando senza console (es. lanciato con UAC dall'app grafica) scrive il proprio esito."""
    import tempfile
    return Path(tempfile.gettempdir()) / "AutomationProjectServer-last-command.log"


def service_command(*args: str) -> tuple[str, str, Optional[str]]:
    """(file, parametri, cartella) per lanciare un comando del servizio: lo stesso exe dell'app (o ``python -m`` in sviluppo)."""
    def q(a: str) -> str:
        return f'"{a}"' if (" " in a or not a) else a
    if getattr(sys, "frozen", False):
        return sys.executable, " ".join(q(a) for a in args), str(Path(sys.executable).parent)
    root = Path(__file__).resolve().parent.parent
    return sys.executable, " ".join(["-m", "server.launcher", *(q(a) for a in args)]), str(root)


def run_elevated(file: str, params: str, cwd: Optional[str] = None) -> bool:
    """Lancia ``file params`` con richiesta UAC (finestra del prompt). Ritorna False se l'utente rifiuta o non e' Windows."""
    if not is_windows():
        return False
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", file, params, cwd, 1)
    return rc > 32
