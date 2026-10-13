"""Aggiornamento automatico del client: il server ospita l'ultimo eseguibile del client (``<dati>/updates/``) e i client
lo scaricano, ne verificano lo SHA-256 e si sostituiscono da soli (vedi ``client/updater.py``)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from common.roles import Role
from common.schemas_inbox import UpdateInfo
from common.version import is_newer
from server.auth.models import Principal
from server.auth.rbac import get_principal, min_role

META = "version.json"


def updates_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "updates"


def read_info(data_dir: Path) -> UpdateInfo:
    p = updates_dir(data_dir) / META
    if not p.exists():
        return UpdateInfo()
    try:
        d = json.loads(p.read_text("utf-8"))
        info = UpdateInfo(**d)
    except (OSError, ValueError, TypeError):
        return UpdateInfo()
    return info if (updates_dir(data_dir) / info.filename).exists() else UpdateInfo()


def publish(data_dir: Path, src: Path, version: str, notes: str = "", actor: str = "console", filename: str = "") -> UpdateInfo:
    """Copia ``src`` come client pubblicato (sostituisce il precedente). Ritorna le informazioni salvate."""
    version = version.strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+){0,3}", version):
        raise ValueError("La versione deve essere del tipo 2.1 o 2.1.3 (solo numeri separati da punti)")
    src = Path(src)
    if not src.is_file() or src.stat().st_size < 1024:
        raise ValueError("File del client non valido (vuoto o troppo piccolo)")
    d = updates_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or src.name) or "AutomationProjectClient.exe"
    h = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    tmp = d / (name + ".tmp")
    shutil.copyfile(src, tmp)
    old = read_info(data_dir)
    os.replace(tmp, d / name)
    if old.filename and old.filename != name:
        (d / old.filename).unlink(missing_ok=True)
    info = UpdateInfo(available=False, version=version, notes=notes.strip()[:2000], sha256=h.hexdigest(),
                      size=(d / name).stat().st_size, filename=name, published_at=datetime.now(timezone.utc).isoformat(),
                      published_by=actor)
    (d / META).write_text(info.model_dump_json(indent=2), "utf-8")
    return info


def remove(data_dir: Path) -> bool:
    d = updates_dir(data_dir)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def build_router(audit) -> APIRouter:                                     # noqa: ANN001  (AuditService)
    r = APIRouter(prefix="/api/client", tags=["aggiornamento"])

    def data_dir(request: Request) -> Path:
        return request.app.state.settings.data_dir

    @r.get("/update", response_model=UpdateInfo)
    def check(request: Request, current: str = Query(""), _: Principal = Depends(get_principal)):
        info = read_info(data_dir(request))
        info.available = bool(info.version) and (not current or is_newer(info.version, current))
        return info

    @r.get("/download")
    def download(request: Request, _: Principal = Depends(get_principal)):
        info = read_info(data_dir(request))
        if not info.version:
            raise HTTPException(404, "Nessun aggiornamento del client pubblicato")
        return FileResponse(updates_dir(data_dir(request)) / info.filename, media_type="application/octet-stream",
                            filename=info.filename)

    @r.post("/update", response_model=UpdateInfo, status_code=201)
    async def publish_update(request: Request, file: UploadFile = File(...), version: str = Form(...), notes: str = Form(""),
                             p: Principal = Depends(min_role(Role.ADMIN))):
        cfg = request.app.state.settings
        tmp = Path(cfg.data_dir) / "updates_upload.tmp"
        size = 0
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > 1024 * 1024 * 1024:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, "File troppo grande")
                out.write(chunk)
        try:
            info = await run_in_threadpool(publish, cfg.data_dir, tmp, version, notes, p.username, file.filename or "")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            tmp.unlink(missing_ok=True)
        audit.record("client_update_published", p.username, ip=p.ip, target=info.version,
                     details={"sha256": info.sha256, "size": info.size, "filename": info.filename})
        return info

    @r.delete("/update", status_code=204)
    def unpublish(request: Request, p: Principal = Depends(min_role(Role.ADMIN))):
        if remove(data_dir(request)):
            audit.record("client_update_removed", p.username, ip=p.ip)

    return r
