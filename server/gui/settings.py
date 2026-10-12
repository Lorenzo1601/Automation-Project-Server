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
