"""Ricerca dei server in rete locale (UDP broadcast). Senza dipendenze Qt: va eseguita in un thread di lavoro."""
from __future__ import annotations

import select
import socket
import time
from dataclasses import dataclass
from typing import Optional

from common.discovery import DISCOVERY_PORT, PROBE, parse_reply


@dataclass(frozen=True)
class ServerInfo:
    name: str
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.host}:{self.port})"


def local_ipv4() -> list[str]:
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    try:                                              # interfaccia di uscita predefinita (non invia pacchetti)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip not in out and not ip.startswith("127."):
                out.insert(0, ip)
    except OSError:
        pass
    return out


def _broadcast_targets(ip: str, port: int) -> list[tuple[str, int]]:
    a, b, c, _ = ip.split(".")
    return [("255.255.255.255", port), (f"{a}.{b}.{c}.255", port)]        # subnet /24 comune; il resto lo copre il globale


def discover(timeout: float = 1.5, udp_port: int = DISCOVERY_PORT,
             targets: Optional[list[tuple[str, int]]] = None) -> list[ServerInfo]:
    """Cerca i server e ritorna l'elenco (senza duplicati, ordinato per nome).

    Se ``targets`` e' indicato si interrogano solo quegli indirizzi (utile nei test); altrimenti si fa broadcast da ogni
    interfaccia IPv4 della macchina e si prova anche ``127.0.0.1`` (server sullo stesso PC).
    """
    socks: list[socket.socket] = []
    plan: list[tuple[socket.socket, tuple[str, int]]] = []

    def make(bind_ip: str = "") -> Optional[socket.socket]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((bind_ip, 0))
        except OSError:
            s.close()
            return None
        s.setblocking(False)
        socks.append(s)
        return s

    try:
        if targets is not None:
            s = make()
            if s:
                plan += [(s, t) for t in targets]
        else:
            s0 = make()
            if s0:
                plan.append((s0, ("127.0.0.1", udp_port)))
                plan.append((s0, ("255.255.255.255", udp_port)))
            for ip in local_ipv4():
                s = make(ip)
                if s:
                    plan += [(s, t) for t in _broadcast_targets(ip, udp_port)]
        for s, t in plan:
            try:
                s.sendto(PROBE, t)
            except OSError:
                pass
        found: dict[str, ServerInfo] = {}
        end = time.monotonic() + timeout
        while socks and (left := end - time.monotonic()) > 0:
            ready, _, _ = select.select(socks, [], [], min(left, 0.2))
            for s in ready:
                try:
                    data, addr = s.recvfrom(1024)
                except OSError:
                    continue
                r = parse_reply(data)
                if r:
                    # lo stesso server risponde da piu' indirizzi (127.0.0.1 e IP di rete): si tiene uno solo, preferendo l'IP di rete
                    key = r["id"] or f"{addr[0]}:{r['port']}"
                    cur = found.get(key)
                    if cur is None or (cur.host.startswith("127.") and not addr[0].startswith("127.")):
                        found[key] = ServerInfo(r["name"], addr[0], r["port"])
        return sorted(found.values(), key=lambda i: (i.name.lower(), i.host))
    finally:
        for s in socks:
            s.close()
