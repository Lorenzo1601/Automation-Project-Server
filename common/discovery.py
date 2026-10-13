"""Protocollo di scoperta del server in rete locale (UDP broadcast, nessuna dipendenza esterna).

Il client invia ``PROBE`` in broadcast sulla porta UDP ``DISCOVERY_PORT``; ogni server risponde (unicast) con un JSON
che contiene nome, porta e schema (``http``/``https``). L'indirizzo IP e' quello da cui arriva la risposta.
"""
from __future__ import annotations

import json
from typing import Optional

DISCOVERY_PORT = 8751
PROBE = b"APS-DISCOVER/1"
APP_ID = "AutomationProjectServer"


def make_reply(name: str, http_port: int, sid: str = "", version: str = "2.0", scheme: str = "http") -> bytes:
    """``scheme``: ``http`` o ``https`` (campo opzionale: i client vecchi lo ignorano, quelli nuovi senza campo assumono http)."""
    return json.dumps({"app": APP_ID, "v": version, "name": name, "port": int(http_port), "id": sid,
                       "scheme": "https" if scheme == "https" else "http"},
                      ensure_ascii=False).encode("utf-8")


def parse_reply(data: bytes) -> Optional[dict]:
    """Ritorna il dizionario della risposta oppure None se il pacchetto non e' di un nostro server."""
    try:
        d = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or d.get("app") != APP_ID:
        return None
    try:
        port = int(d["port"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < port < 65536:
        return None
    return {"name": str(d.get("name") or "Server")[:80], "port": port, "version": str(d.get("v", "")),
            "id": str(d.get("id") or "")[:64], "scheme": "https" if d.get("scheme") == "https" else "http"}
