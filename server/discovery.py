"""Risponditore UDP per la scoperta automatica del server da parte dei client (vedi ``common/discovery.py``)."""
from __future__ import annotations

import logging
import socket
import threading
import uuid
from typing import Optional

from common.discovery import DISCOVERY_PORT, PROBE, make_reply

log = logging.getLogger("aps.gui")


class DiscoveryResponder:
    def __init__(self, http_port: int, name: str, udp_port: int = DISCOVERY_PORT, bind: str = "",
                 scheme: str = "http") -> None:
        self.http_port, self.name, self.udp_port, self.bind = http_port, name, udp_port, bind
        self.scheme = scheme                       # "https" quando il server usa TLS: i client costruiscono l'URL giusto
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_error = ""
        self.sid = uuid.uuid4().hex                # identifica questo server anche se risponde da piu' indirizzi

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running:
            return True
        if self.udp_port <= 0:
            return False
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.bind, self.udp_port))
        except OSError as exc:
            s.close()
            self.last_error = f"Scoperta automatica non attiva (UDP {self.udp_port}): {exc.strerror or exc}"
            log.warning(self.last_error)
            return False
        s.settimeout(0.4)
        self._sock, self._stop = s, threading.Event()
        self._thread = threading.Thread(target=self._loop, name="aps-discovery", daemon=True)
        self._thread.start()
        log.info("Scoperta automatica attiva (UDP %s) come '%s'", self.udp_port, self.name)
        return True

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() == PROBE:
                try:
                    self._sock.sendto(make_reply(self.name, self.http_port, self.sid, scheme=self.scheme), addr)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        if self._sock:
            self._sock.close()
        self._thread = self._sock = None
