"""Tipi di file gestiti dal Master (condiviso da server e client, nessuna dipendenza esterna).

Il Master puo' essere *qualsiasi* file: il server lo versiona sempre (revisioni, download, ripristino). Il tipo decide
solo come si fa il merge:

* ``l5x``    Rockwell XML (Studio 5000)   -> merge strutturale (Rung / Line / Tag ...)
* ``l5k``    Rockwell ASCII (RSLogix 5000) -> merge testuale per riga, con contesto Program/Routine
* ``text``   qualsiasi file di testo (es. ``.st``, ``.xml``, ``.project`` di altri IDE) -> merge testuale per riga
* ``binary`` qualsiasi altro file       -> solo versionamento: le richieste di merge non sono possibili
"""
from __future__ import annotations

import re
from pathlib import PurePath

L5X, L5K, TEXT, BINARY = "l5x", "l5k", "text", "binary"
KINDS = (L5X, L5K, TEXT, BINARY)

ROCKWELL_EXTS = (".L5K", ".L5X")             # K = export ASCII, X = export XML
MERGEABLE = (L5X, L5K, TEXT)

KIND_LABELS = {L5X: "Rockwell L5X (XML)", L5K: "Rockwell L5K (ASCII)", TEXT: "File di testo", BINARY: "File binario"}

_IE_VER = re.compile(rb"(?m)^\s*IE_VER\s*:=")
_L5K_CONTROLLER = re.compile(r"(?m)^\s*CONTROLLER\s+(\w+)\s*\(")
_HEAD = 8192


def detect_kind(data: bytes) -> str:
    """Riconosce il tipo dal *contenuto* (l'estensione da sola non basta: un .L5X puo' essere un file qualsiasi)."""
    head = data[:_HEAD]
    if b"RSLogix5000Content" in head[:4096]:
        return L5X
    if _IE_VER.search(head) and b"CONTROLLER" in data[:200_000]:
        return L5K
    return BINARY if b"\x00" in data[:65536] else TEXT


def canonical_ext(filename: str, kind: str) -> str:
    """Estensione con cui il server salva il Master: ``.L5X`` / ``.L5K`` per i Rockwell, altrimenti quella del file."""
    if kind == L5X:
        return ".L5X"
    if kind == L5K:
        return ".L5K"
    ext = PurePath(filename or "").suffix
    ext = "".join(c for c in ext if c.isalnum() or c == ".")[:12]
    if len(ext) < 2:
        ext = ".txt" if kind == TEXT else ".bin"
    return ext


def l5k_controller_name(data: bytes) -> str:
    m = _L5K_CONTROLLER.search(data[:200_000].decode("latin-1"))
    return m.group(1) if m else ""


def media_type(kind: str) -> str:
    return {L5X: "application/xml", L5K: "text/plain", TEXT: "text/plain"}.get(kind, "application/octet-stream")


def file_dialog_filters(ext: str = "", kind: str = "") -> str:
    """Filtri per ``QFileDialog``: il tipo del progetto per primo, poi Rockwell, poi *Tutti i file*."""
    out: list[str] = []
    if ext:
        out.append(f"File del progetto (*{ext.upper()} *{ext.lower()})")
    out.append("Rockwell (*.L5K *.L5X *.l5k *.l5x)")
    out.append("Tutti i file (*)")
    return ";;".join(out)


def import_filters() -> str:
    """Filtri per scegliere il Master iniziale: tutti i file in testa, e Rockwell L5K/L5X a portata di mano."""
    return "Tutti i file (*);;Rockwell (*.L5K *.L5X *.l5k *.l5x);;Rockwell L5K (*.L5K *.l5k);;Rockwell L5X (*.L5X *.l5x)"
