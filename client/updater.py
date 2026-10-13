"""Aggiornamento automatico del client: scarica l'eseguibile pubblicato dal server, ne verifica lo SHA-256 e, chiusa
l'applicazione, lo sostituisce con un piccolo script e la riavvia. Funziona solo dall'eseguibile (PyInstaller)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from client.api_client import ApiClient
from common.schemas_inbox import UpdateInfo
from common.version import APP_VERSION


class UpdateError(Exception):
    pass


def running_exe() -> Optional[Path]:
    """Percorso dell'eseguibile in uso se l'app e' «congelata» (PyInstaller), altrimenti None (da sorgenti non si aggiorna)."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def check(api: ApiClient, current: str = APP_VERSION) -> UpdateInfo:
    return api.client_update(current)


def download(api: ApiClient, info: UpdateInfo, dest_dir: Optional[Path] = None) -> Path:
    """Scarica il nuovo client in una cartella temporanea e ne verifica lo SHA-256 dichiarato dal server."""
    dest_dir = Path(dest_dir or tempfile.mkdtemp(prefix="aps_update_"))
    dest = dest_dir / (info.filename or "AutomationProjectClient.exe")
    api.download_client(dest)
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if info.sha256 and got != info.sha256:
        dest.unlink(missing_ok=True)
        raise UpdateError("Il file scaricato non corrisponde allo SHA-256 dichiarato dal server: aggiornamento annullato.")
    return dest


def replace_script(new_exe: Path, target: Path, pid: Optional[int] = None) -> str:
    """Testo dello script .bat che attende la chiusura dell'app, sostituisce l'eseguibile e lo riavvia."""
    pid = pid if pid is not None else os.getpid()
    return "\r\n".join([
        "@echo off",
        f'rem Aggiornamento Automation Project Client -> {target.name}',
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
        "if not errorlevel 1 (ping 127.0.0.1 -n 2 >nul & goto wait)",
        "set tries=0",
        ":move",
        f'move /Y "{new_exe}" "{target}" >nul 2>&1',
        "if errorlevel 1 (set /a tries+=1 & if %tries% LSS 30 (ping 127.0.0.1 -n 2 >nul & goto move))",
        f'start "" "{target}"',
        'del "%~f0"', ""])


def apply(new_exe: Path, launcher: Callable[..., object] = subprocess.Popen) -> Path:
    """Prepara e avvia lo script di sostituzione (Windows). Chi chiama deve poi chiudere l'applicazione.
    Ritorna il percorso dello script."""
    target = running_exe()
    if target is None:
        raise UpdateError("L'aggiornamento automatico funziona solo dall'eseguibile del client (non dai sorgenti).")
    bat = Path(tempfile.gettempdir()) / "aps_client_update.bat"
    bat.write_text(replace_script(Path(new_exe), target), "utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    launcher(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)
    return bat
