"""Backup della cartella dati del server in un unico .zip (senza dipendenze Qt).

Il file contiene il database (copia consistente fatta con l'API di backup di SQLite: funziona a server acceso, anche con
il WAL aperto), tutti i progetti (``projects/``: revisioni, storico, proposte) e il segreto JWT (``jwt_secret``).
Escluse le tracce temporanee (``*.tmp``, ``*-wal``, ``*-shm``, cartelle ``.deleted-*``).

ATTENZIONE: il backup contiene i dati dei progetti, gli hash delle password e il SEGRETO DI FIRMA DEI TOKEN: chi lo
possiede puo' creare token validi. Va conservato in un posto protetto (cartella con permessi ristretti, disco cifrato).

Ripristino a mano: fermare il server (e il servizio Windows), svuotare/spostare la cartella dati e scompattare l'archivio
dentro la cartella dati (deve contenere ``aps.db`` e ``projects/``), poi riavviare. ``BACKUP_INFO.txt`` nell'archivio lo ricorda.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

DB_NAME = "aps.db"
_SKIP_SUFFIX = (".tmp", "-wal", "-shm")


def default_backup_name() -> str:
    return f"aps_backup_{datetime.now():%Y%m%d_%H%M}.zip"


def _info(data_dir: Path) -> str:
    return (f"Backup di Automation Project Server\nCreato il {datetime.now():%d/%m/%Y alle %H:%M}\n"
            f"Cartella dati di origine: {data_dir}\n\n"
            "CONTIENE I DATI DEI PROGETTI E IL SEGRETO JWT (jwt_secret): conservalo in un posto protetto.\n\n"
            "Come ripristinarlo a mano:\n"
            "  1. ferma il server (e il servizio Windows, se installato);\n"
            "  2. sposta o svuota la cartella dati del server;\n"
            "  3. scompatta questo archivio dentro la cartella dati (devono comparire aps.db e la cartella projects);\n"
            "  4. riavvia il server.\n")


def _snapshot_db(src: Path, dst: Path) -> None:
    """Copia consistente del database SQLite (anche se un altro processo lo sta usando)."""
    a = sqlite3.connect(src, timeout=30)
    try:
        b = sqlite3.connect(dst)
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()


def create_backup(data_dir: "str | Path", dest: "str | Path") -> dict:
    """Crea lo .zip ``dest`` dalla cartella dati. Ritorna ``{"path", "bytes", "files"}``. Scrittura atomica."""
    data_dir, dest = Path(data_dir).resolve(), Path(dest).resolve()
    db = data_dir / DB_NAME
    if not db.exists():
        raise FileNotFoundError(f"Nessun database in {data_dir}: niente da salvare")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    n = 0
    try:
        with tempfile.TemporaryDirectory(prefix="aps_bk_") as td:
            snap = Path(td) / DB_NAME
            _snapshot_db(db, snap)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, DB_NAME)
                n += 1
                for root, dirs, names in os.walk(data_dir):                       # followlinks=False: niente link simbolici
                    dirs[:] = [d for d in dirs if not d.startswith(".deleted-") and not (Path(root) / d).is_symlink()]
                    for name in names:
                        f = Path(root) / name
                        rel = f.relative_to(data_dir)
                        if (f == db or f.name.endswith(_SKIP_SUFFIX) or f.resolve() in (dest, tmp)
                                or f.is_symlink() or str(rel) == "BACKUP_INFO.txt"):
                            continue
                        z.write(f, rel.as_posix())
                        n += 1
                z.writestr("BACKUP_INFO.txt", _info(data_dir))
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": dest, "bytes": dest.stat().st_size, "files": n}
