"""Ricezione notifiche push dal server via QWebSocket, con riconnessione automatica.

L'URL (che contiene il token di accesso e il progetto) viene ricalcolato ad ogni tentativo tramite una factory,
cosi' dopo un refresh del token o un cambio di progetto la riconnessione usa sempre valori aggiornati.
"""
from __future__ import annotations

import json
from typing import Callable, Union

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket


class WsListener(QObject):
    event_received = Signal(dict)
    connection_changed = Signal(bool)
    state_changed = Signal(str)               # online | connecting | offline

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._factory: Callable[[], str] = lambda: ""
        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.errorOccurred.connect(self._on_error)
        self._want = False
        self._retry = QTimer(self, interval=5000, singleShot=True)
        self._retry.timeout.connect(self._open)
        self._ping = QTimer(self, interval=20000)
        self._ping.timeout.connect(self._send_ping)
        self._ping.start()

    @property
    def is_up(self) -> bool:
        return self._ws.isValid()

    def start(self, url: Union[str, Callable[[], str]]) -> None:
        """Avvia (o riavvia, ad esempio dopo il cambio di progetto) la connessione."""
        self._factory = url if callable(url) else (lambda u=url: u)
        self._want = True
        self._retry.stop()
        if self._ws.state() != self._ws.state().UnconnectedState:
            self._ws.abort()
        self._open()

    def stop(self) -> None:
        self._want = False
        self._retry.stop()
        self._ws.abort()
        self.state_changed.emit("offline")

    def _open(self) -> None:
        if self._want and not self._ws.isValid():
            self.state_changed.emit("connecting")
            self._ws.open(QUrl(self._factory()))

    def _send_ping(self) -> None:
        if self._ws.isValid():
            self._ws.sendTextMessage("ping")

    def _on_connected(self) -> None:
        self.connection_changed.emit(True)
        self.state_changed.emit("online")

    def _on_disconnected(self) -> None:
        self.connection_changed.emit(False)
        if self._want:
            self.state_changed.emit("connecting")
            self._retry.start()
        else:
            self.state_changed.emit("offline")

    def _on_error(self, _err) -> None:                     # noqa: ANN001
        # un'apertura fallita (server giu', token scaduto) potrebbe non emettere 'disconnected'
        if self._want and not self._ws.isValid() and not self._retry.isActive():
            self.state_changed.emit("connecting")
            self._retry.start()

    def _on_text(self, text: str) -> None:
        if text == "pong":
            return
        try:
            self.event_received.emit(json.loads(text))
        except json.JSONDecodeError:
            pass
