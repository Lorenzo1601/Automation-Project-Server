"""Versione dell'applicazione (client e server) e confronto tra versioni «a.b.c»."""
from __future__ import annotations

import re

APP_VERSION = "2.0"


def version_tuple(v: str) -> tuple[int, ...]:
    """'2.10.1' -> (2, 10, 1); le parti non numeriche vengono ignorate ('2.0-beta' -> (2, 0))."""
    parts = []
    for p in re.split(r"[.\-+_ ]", (v or "").strip().lstrip("vV")):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    a, b = version_tuple(candidate), version_tuple(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))
