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

    def publish(self, event: dict, targets: Optional[Iterable[str]] = None, scope: Optional[str] = None,
                only: Optional[str] = None) -> None:
        """``targets``: chiavi connessione esatte; ``scope``: prefisso (broadcast a un progetto).

        Le chiavi dei client di un branch sono ``<pid>:@<branch>:u<id>``, quelle del principale ``<pid>:u<id>``.
        ``only`` restringe un broadcast di progetto: ``"main"`` = solo client del principale, ``"branches"`` = solo client
        di un branch qualsiasi (``None`` = tutti i client del progetto).
        """
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._send(event, None if targets is None else set(targets), scope, only), self._loop)

    def close_project(self, project_id: int, event: dict, timeout: float = 3.0) -> None:
        """Invia ``event`` a tutti i client di un progetto e ne chiude le connessioni (es. progetto eliminato).

        Attende (al piu' ``timeout`` s) che l'invio sia avvenuto, cosi' chi chiama sa che i client sono stati avvisati.
        """
        if self._loop is None or self._loop.is_closed():
            return
        self.close_scope(f"{project_id}:", event, timeout)

    def close_scope(self, prefix: str, event: dict, timeout: float = 3.0) -> None:
        """Come ``close_project`` ma per un prefisso qualsiasi (es. ``"3:@prova:"`` = i client di un solo branch)."""
        if self._loop is None or self._loop.is_closed():
            return
        fut = asyncio.run_coroutine_threadsafe(self._close_scope(prefix, event), self._loop)
        try:
            fut.result(timeout)
        except Exception:                                       # noqa: BLE001  (timeout o loop in chiusura)
            log.info("Chiusura delle connessioni %s non completata", prefix)

    async def _close_scope(self, prefix: str, event: dict) -> None:
        with self._lock:
            socks = [(sid, ws) for sid, group in self._conns.items() if sid.startswith(prefix) for ws in list(group)]
        for sid, ws in socks:
            try:
                await ws.send_json(event)
                await ws.close(code=4404)
            except Exception:                                   # connessione gia' morta
                pass
            self.unregister(sid, ws)

    @staticmethod
    def _in_scope(sid: str, scope: Optional[str], only: Optional[str]) -> bool:
        if scope is None:
            return True
        if not sid.startswith(scope):
            return False
        if only is None:
            return True
        is_branch = sid[len(scope):].startswith("@")
        return is_branch if only == "branches" else not is_branch

    async def _send(self, event: dict, targets: Optional[set[str]], scope: Optional[str] = None,
                    only: Optional[str] = None) -> None:
        with self._lock:
            socks = [(sid, ws) for sid, group in self._conns.items()
                     if (sid in targets if targets is not None else self._in_scope(sid, scope, only))
                     for ws in list(group)]
        for sid, ws in socks:
            try:
                await ws.send_json(event)
            except Exception:                                   # connessione morta
                log.info("WS %s non raggiungibile, rimosso", sid)
                self.unregister(sid, ws)


class ProjectSink:
    """Limita le notifiche del PRINCIPALE di un progetto ai soli client connessi al principale di quel progetto.

    Quando il principale avanza (``master_updated``) avvisa anche i client dei branch con ``main_updated``.
    """

    def __init__(self, notifier: Notifier, project_id: int):
        self.notifier, self.prefix = notifier, f"{project_id}:"

    def publish(self, event: dict, targets: Optional[Iterable[str]] = None) -> None:
        if targets is None:
            self.notifier.publish(event, None, scope=self.prefix, only="main")
            if event.get("type") == "master_updated":
                self.notifier.publish({**event, "type": "main_updated"}, None, scope=self.prefix, only="branches")
        else:
            self.notifier.publish(event, {self.prefix + t for t in targets})


class BranchSink:
    """Notifiche di un branch: marca ogni evento con lo slug del branch e lo recapita solo ai client iscritti a quel branch."""

    def __init__(self, notifier: Notifier, project_id: int, branch: str):
        self.notifier, self.branch = notifier, branch
        self.prefix = branch_key_prefix(project_id, branch)

    def publish(self, event: dict, targets: Optional[Iterable[str]] = None) -> None:
        event = {**event, "branch": self.branch}
        if targets is None:
            self.notifier.publish(event, None, scope=self.prefix)
        else:
            self.notifier.publish(event, {self.prefix + t for t in targets})


def branch_key_prefix(project_id: int, branch: str) -> str:
    """Prefisso delle chiavi WebSocket dei client di un branch (``@`` non puo' comparire in uno slug)."""
    return f"{project_id}:@{branch}:"
