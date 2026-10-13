"""Preferenze della GUI del server (QSettings): cartella dati, indirizzo/porta, avvio automatico."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings

DEFAULT_PORT = 8750


class ServerPrefs:
    def __init__(self, qs: Optional[QSettings] = None) -> None:
        self.qs = qs or QSettings("AutomationProjectServer", "ServerConsole2")

    # cartella scelta dall'utente (il server vi crea la sottocartella AutomationProjectServer)
    @property
    def data_base(self) -> str:
        return str(self.qs.value("data_base", "") or "")

    @data_base.setter
    def data_base(self, v: str) -> None:
        self.qs.setValue("data_base", v)

    @property
    def host(self) -> str:
        return str(self.qs.value("host", "0.0.0.0"))

    @host.setter
    def host(self, v: str) -> None:
        self.qs.setValue("host", v)

    @property
    def port(self) -> int:
        try:
            return int(self.qs.value("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            return DEFAULT_PORT

    @port.setter
    def port(self, v: int) -> None:
        self.qs.setValue("port", int(v))

    @property
    def autostart(self) -> bool:
        return str(self.qs.value("autostart", "true")).lower() in ("true", "1")

    @autostart.setter
    def autostart(self, v: bool) -> None:
        self.qs.setValue("autostart", bool(v))

    @property
    def needs_setup(self) -> bool:
        return not self.data_base

    @property
    def server_name(self) -> str:
        return str(self.qs.value("server_name", "") or "")

    @server_name.setter
    def server_name(self, v: str) -> None:
        self.qs.setValue("server_name", v)

    # --- connessione sicura (HTTPS): vedi server/tls.py. tls_mode: "self" (autofirmato in <dati>/tls) | "custom" (PEM dell'utente)
    @property
    def tls_enabled(self) -> bool:
        return str(self.qs.value("tls_enabled", "false")).lower() in ("true", "1")

    @tls_enabled.setter
    def tls_enabled(self, v: bool) -> None:
        self.qs.setValue("tls_enabled", bool(v))

    @property
    def tls_mode(self) -> str:
        return "custom" if str(self.qs.value("tls_mode", "self")) == "custom" else "self"

    @tls_mode.setter
    def tls_mode(self, v: str) -> None:
        self.qs.setValue("tls_mode", "custom" if v == "custom" else "self")

    @property
    def tls_certfile(self) -> str:
        return str(self.qs.value("tls_certfile", "") or "")

    @tls_certfile.setter
    def tls_certfile(self, v: str) -> None:
        self.qs.setValue("tls_certfile", v or "")

    @property
    def tls_keyfile(self) -> str:
        return str(self.qs.value("tls_keyfile", "") or "")

    @tls_keyfile.setter
    def tls_keyfile(self, v: str) -> None:
        self.qs.setValue("tls_keyfile", v or "")
