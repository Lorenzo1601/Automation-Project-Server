"""Push WebSocket verso i client. ``publish`` e' thread-safe (chiamato dai worker sync di FastAPI)."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Iterable, Optional, Protocol

from fastapi import WebSocket

log = logging.getLogger("aps.notifier")


class EventSink(Protocol):
    def publish(self, event: dict, targets: Optional[Iterable[str]] = None) -> None: ...


class NullSink:
    def publish(self, event: dict, targets: Optional[Iterable[str]] = None) -> None:
        pass


class RecordingSink:
    """Utile nei test: accumula (event, targets)."""
    def __init__(self) -> None:
        self.events: list[tuple[dict, Optional[set[str]]]] = []

    def publish(self, event: dict, targets: Optional[Iterable[str]] = None) -> None:
        self.events.append((event, None if targets is None else set(targets)))


class Notifier:
    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def connection_count(self) -> int:
        """Numero di WebSocket aperti (client collegati in tempo reale)."""
        with self._lock:
            return sum(len(g) for g in self._conns.values())

    def register(self, session_id: str, ws: WebSocket) -> None:
        with self._lock:
            self._conns.setdefault(session_id, set()).add(ws)

    def unregister(self, session_id: str, ws: WebSocket) -> None:
        with self._lock:
            s = self._conns.get(session_id)
            if s:
                s.discard(ws)
                if not s:
                    self._conns.pop(session_id, None)

    def publish(self, event: dict, targets: Optional[Iterable[str]] = None, scope: Optional[str] = None) -> None:
        """``targets``: chiavi connessione esatte; ``scope``: prefisso (broadcast a un progetto)."""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._send(event, None if targets is None else set(targets), scope), self._loop)

    async def _send(self, event: dict, targets: Optional[set[str]], scope: Optional[str] = None) -> None:
        with self._lock:
            socks = [(sid, ws) for sid, group in self._conns.items()
                     if (sid in targets if targets is not None else (scope is None or sid.startswith(scope)))
                     for ws in list(group)]
        for sid, ws in socks:
            try:
                await ws.send_json(event)
            except Exception:                                   # connessione morta
                log.info("WS %s non raggiungibile, rimosso", sid)
                self.unregister(sid, ws)


class ProjectSink:
    """Limita le notifiche di un progetto ai soli client connessi a quel progetto."""

    def __init__(self, notifier: Notifier, project_id: int):
        self.notifier, self.prefix = notifier, f"{project_id}:"

    def publish(self, event: dict, targets: Optional[Iterable[str]] = None) -> None:
        if targets is None:
            self.notifier.publish(event, None, scope=self.prefix)
        else:
            self.notifier.publish(event, {self.prefix + t for t in targets})
