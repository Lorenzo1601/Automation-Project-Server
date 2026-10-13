"""Ricezione notifiche push dal server via QWebSocket, con riconnessione automatica.

L'URL (che contiene il token di accesso e il progetto) viene ricalcolato ad ogni tentativo tramite una factory,
cosi' dopo un refresh del token o un cambio di progetto la riconnessione usa sempre valori aggiornati.
"""
from __future__ import annotations

import json
from typing import Callable, Optional, Union

from PySide6.QtCore import QByteArray, QCryptographicHash, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QSsl, QSslCertificate, QSslConfiguration
from PySide6.QtWebSockets import QWebSocket

from client import trust as _trust


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
        self._ws.sslErrors.connect(self._on_ssl_errors)
        self.trust: Optional[_trust.TrustStore] = None       # archivio dei pin (default: file nella configurazione utente)
        self._pin = ""                                        # impronta attesa per la connessione wss corrente
        self.last_ssl_error = ""
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
            qurl = QUrl(self._factory())
            self._configure_tls(qurl)
            self._ws.open(qurl)

    def _configure_tls(self, qurl: QUrl) -> None:
        """wss: con pin salvato l'unica CA attendibile e' il certificato fissato; senza pin vale la fiducia di sistema."""
        self._pin = ""
        if qurl.scheme().lower() != "wss":
            return
        store = self.trust or _trust.default_store()
        host, port = qurl.host(), qurl.port(443)
        self._pin = store.get(host, port) or ""
        cfg = QSslConfiguration.defaultConfiguration()
        der = store.get_der(host, port) if self._pin else None
        if der:
            cfg.setCaCertificates([QSslCertificate(QByteArray(der), QSsl.EncodingFormat.Der)])
        self._ws.setSslConfiguration(cfg)

    def _on_ssl_errors(self, errors) -> None:                                           # noqa: ANN001
        """Ignora gli errori (autofirmato, nome host) SOLO se il certificato ha l'impronta fissata dall'utente."""
        self.last_ssl_error = ""
        if self._pin and errors:
            def fp(e) -> str:                                                           # noqa: ANN001
                c = e.certificate()
                return "" if c.isNull() else bytes(c.digest(QCryptographicHash.Algorithm.Sha256).toHex().data()).decode()
            if all(fp(e) == self._pin for e in errors):
                self._ws.ignoreSslErrors()
                return
            self.last_ssl_error = _trust.changed_message(self._ws.requestUrl().host(), self._ws.requestUrl().port(443))
        else:
            self.last_ssl_error = "Certificato del server non attendibile: accedi di nuovo per verificarlo."

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
