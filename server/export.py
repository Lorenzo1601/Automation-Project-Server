"""Esportazione di un progetto in uno .zip: revisioni conservate, storico, audit del progetto, README."""
from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime

from common.filekinds import rev_filename
from server.db.models import AuditLog
from server.storage import MasterStore


def audit_csv(rows: list[AuditLog]) -> bytes:
    """CSV (UTF-8 con BOM, apribile con Excel): id, ts, actor, action, target, details (JSON)."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["id", "ts", "actor", "action", "target", "details"])
    for r in rows:
        w.writerow([r.id, r.ts, r.actor, r.action, r.target, r.details])
    return buf.getvalue().encode("utf-8-sig")


def readme(name: str, slug: str, store: MasterStore, max_revisions: int, n_audit: int) -> str:
    hist = store.history()
    kept = [r for r in hist if not r.purged]
    gone = [r.revision for r in hist if r.purged]
    lines = [
        f"ESPORTAZIONE DEL PROGETTO «{name}»",
        f"Generata il {datetime.now().strftime('%d/%m/%Y alle %H:%M')} da Automation Project Server",
        "",
        "Contenuto dell'archivio:",
        f"  - {len(kept)} revisioni del Master (file <nome>_revN<estensione>), l'ultima e' la rev {hist[-1].revision}"
        if hist else "  - nessuna revisione (Master non ancora inizializzato)",
        "  - history.json: metadati di tutte le revisioni (autore, data, messaggio, SHA-256, flag 'purged')",
        f"  - audit.csv: {n_audit} eventi del registro di audit relativi a questo progetto",
        "",
        f"Il server conserva al massimo {max_revisions} revisioni per progetto.",
    ]
    if gone:
        lines.append("Revisioni ELIMINATE dal limite (presenti in history.json con purged=true, file non disponibili): "
                     + ", ".join(str(n) for n in gone))
    else:
        lines.append("Nessuna revisione e' stata eliminata dal limite: lo storico e' completo.")
    lines += ["", f"Identificativo del progetto sul server: {slug}", ""]
    return "\n".join(lines)


def build_project_zip(name: str, slug: str, store: MasterStore, audit_rows: list[AuditLog], max_revisions: int,
                     branches: "list[tuple[dict, MasterStore]] | None" = None):
    """Crea lo .zip in un file temporaneo (in memoria fino a 64 MB) e lo ritorna riposizionato all'inizio.

    ``branches``: (metadati, store) dei branch; finiscono in ``branches/<slug>/`` (revisioni conservate + history.json)
    e nell'elenco ``branches.json``.
    """
    out = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        hist = store.history()
        for r in hist:
            if not r.purged:
                z.writestr(rev_filename(r.name, r.revision, r.ext), store.read_revision(r.revision))
        z.writestr("history.json", json.dumps([asdict(r) for r in hist], indent=2, ensure_ascii=False))
        z.writestr("audit.csv", audit_csv(audit_rows))
        if branches:
            z.writestr("branches.json", json.dumps([m for m, _ in branches], indent=2, ensure_ascii=False, default=str))
            for meta, bst in branches:
                bh = bst.history()
                for r in bh:
                    if not r.purged:
                        z.writestr(f"branches/{meta['slug']}/{rev_filename(r.name, r.revision, r.ext)}",
                                   bst.read_revision(r.revision))
                z.writestr(f"branches/{meta['slug']}/history.json",
                           json.dumps([asdict(r) for r in bh], indent=2, ensure_ascii=False))
        txt = readme(name, slug, store, max_revisions, len(audit_rows))
        if branches:
            txt += (f"\nBranch: {len(branches)} (cartella branches/<identificativo>/ con le revisioni conservate e il loro "
                    "history.json; elenco e stato in branches.json).\n")
        z.writestr("README.txt", txt.encode("utf-8"))
    out.seek(0)
    return out
