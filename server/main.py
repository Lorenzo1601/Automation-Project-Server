"""Automation Project Server - API REST + WebSocket (FastAPI) con autenticazione JWT e RBAC.

Avvio:   python -m server --seed-project "Nome" --master samples/Master_rev1_base.L5X   oppure   uvicorn server.main:create_app --factory
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from common.version import APP_VERSION
from common.roles import Role
from common.schemas import (AuditOut, AuditVerifyOut, DecisionIn, DeveloperOut, Event, MasterInfo,
                            PresenceContext, PresenceOut, PreviewOut, ProposalDetail, ProposalSummary, ResolutionsIn, RestoreIn)
from server.auth import routes as auth_routes
from server.auth.models import AuthError, Principal, ProjectAccess
from server.auth.rbac import effective_project_role, get_principal, min_role, project_access
from server.auth.security import TokenService, load_or_create_secret
from server.auth.service import AuthService, aware
from server.config import Settings
from server.db.audit import AuditService
from server.db.base import Database
from server.db.models import Project, User
from common.filekinds import L5K, L5X, PLCOPEN, detect_kind, l5k_controller_name, media_type, rev_filename
from server.merger import L5XError, Resolution, parse_l5x
from server.plcopen import plcopen_identity
from server.inbox import InboxService, build_router as inbox_router
from server.notifier import Notifier
from server.updates import build_router as updates_router
from server.branches import build_router as branches_router, check_write as check_branch_write
from server.projects import BranchNotFound, ProjectGone, ProjectRegistry, ProjectRuntime
from server.projects import build_router as projects_router
from server.revisions_ext import build_router as revisions_router, lock_out, locks_for
from server.session_manager import Developer, SessionError
from server.storage import RevisionPurged

log = logging.getLogger("aps")


@dataclass
class Ctx:
    access: ProjectAccess
    rt: ProjectRuntime            # runtime del principale o, con ``?branch=<slug>``, del branch
    dev: Developer
    branch: Optional[str] = None  # slug del branch su cui opera la richiesta (None = principale)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    cfg = settings or Settings()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_url)
    db.create_all()
    audit = AuditService(db)
    tokens = TokenService(load_or_create_secret(cfg.data_dir, cfg.jwt_secret),
                          timedelta(minutes=cfg.access_ttl_min), timedelta(hours=cfg.refresh_ttl_h))
    auth = AuthService(db, tokens, audit, cfg)
    notifier = Notifier()
    inbox = InboxService(db, cfg, notifier)
    notifier.inbox = inbox
    registry = ProjectRegistry(cfg, db, notifier, audit)
    bootstrap_pw = auth.bootstrap()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        notifier.attach_loop(asyncio.get_running_loop())

        async def tick():
            while True:
                await asyncio.sleep(15)
                for rt in registry.runtimes():
                    await run_in_threadpool(rt.mgr.housekeeping)
        task = asyncio.create_task(tick())
        yield
        task.cancel()
        db.dispose()

    app = FastAPI(title="Automation Project Server", version=APP_VERSION, lifespan=lifespan)
    for k, v in dict(db=db, audit=audit, auth=auth, registry=registry, notifier=notifier, inbox=inbox, settings=cfg,
                     bootstrap_password=bootstrap_pw).items():
        setattr(app.state, k, v)

    @app.exception_handler(SessionError)
    async def _session_error(_, exc: SessionError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    @app.exception_handler(ProjectGone)
    async def _project_gone(_, exc: ProjectGone):
        return JSONResponse(status_code=404, content={"detail": "Progetto inesistente (in eliminazione)"})

    @app.exception_handler(BranchNotFound)
    async def _branch_gone(_, exc: BranchNotFound):
        return JSONResponse(status_code=404, content={"detail": "Branch inesistente"})

    @app.exception_handler(AuthError)
    async def _auth_error(_, exc: AuthError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(auth_routes.build_router())
    app.include_router(inbox_router())
    app.include_router(updates_router(audit))
    app.include_router(projects_router())

    @app.get("/api/health", tags=["server"])
    def health():
        return {"status": "ok", "version": app.version}

    # --------------------------------------------------------------- contesto di progetto
    def ctx(role: Role, use_branch: bool = True):
        """Contesto di progetto. Con ``?branch=<slug>`` (se ``use_branch``) il runtime e' quello del branch: le richieste
        di scrittura (ruolo Developer+) sono permesse solo dove il branch e' aperto e scrivibile dall'utente."""
        access_dep = project_access(role)

        def dep(ac: ProjectAccess = Depends(access_dep), branch: Optional[str] = Query(None)) -> Ctx:
            b = branch if (use_branch and branch) else None
            rt = registry.runtime(ac.project_id, b)                  # BranchNotFound -> 404
            if b and role >= Role.DEVELOPER:
                check_branch_write(registry.branches.get(ac.project_id, b), ac.role, ac.principal.username)
            dev = rt.mgr.join(ac.principal.user_id, ac.principal.username, ac.role)
            return Ctx(ac, rt, dev, b)
        return dep

    def rec(c: Ctx, action: str, target: str = "", **details) -> None:
        if c.branch:
            details = {**details, "branch": c.branch}
        audit.record(action, c.access.principal.username, project_id=c.access.project_id, target=target,
                     ip=c.access.principal.ip, details=details)

    P = "/api/projects/{pid}"

    # confronto revisioni, etichette/blocco, lock esclusivo e check-in dei binari (server/revisions_ext.py)
    app.include_router(revisions_router(ctx, audit, cfg))

    # -------------------------------------------------------------- master
    @app.post(P + "/master/init", response_model=MasterInfo, tags=["master"])
    async def init_master(file: UploadFile = File(...), c: Ctx = Depends(ctx(Role.LEAD))):
        if c.rt.store.initialized:
            raise HTTPException(409, "Master gia' inizializzato")
        data = await file.read()
        if not data:
            raise HTTPException(422, "Il file e' vuoto")
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        kind = detect_kind(data)
        controller = project_name = ""
        try:
            if kind == L5X:                           # un export Studio 5000 deve essere valido (intero Controller)
                controller = parse_l5x(data).find("Controller").get("Name")
            elif kind == L5K:
                controller = l5k_controller_name(data)
            elif kind == PLCOPEN:                     # PLCopen XML: ben formato, senza DOCTYPE/entita', radice <project>
                project_name = plcopen_identity(data)[0]
        except L5XError as exc:
            raise HTTPException(422, str(exc))
        rev = c.rt.store.commit(data, c.access.principal.username, "Revisione iniziale", filename=file.filename or "")
        rec(c, "master_initialized", f"rev{rev.revision}", sha256=rev.sha256, kind=rev.kind, ext=rev.ext, name=rev.name,
            filename=(file.filename or "")[:120], **({"controller": controller} if controller else {}),
            **({"project_name": project_name} if project_name else {}))
        c.rt.mgr.prune_revisions(c.access.principal.username)
        return c.rt.mgr.master_info()

    @app.post(P + "/master/reimport", response_model=MasterInfo, tags=["master"])
    async def reimport_master(file: UploadFile = File(...), reason: str = Form(...), c: Ctx = Depends(ctx(Role.LEAD))):
        """Reimporta il file base su un progetto in corso (nuova revisione; richieste aperte rianalizzate)."""
        if not c.rt.store.initialized:
            raise HTTPException(409, "Master non inizializzato: usa il caricamento iniziale")
        data = await file.read()
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        await run_in_threadpool(c.rt.mgr.reimport, c.dev, data, file.filename or "", reason)
        return c.rt.mgr.master_info()

    @app.get(P + "/master", response_model=MasterInfo, tags=["master"])
    def master(c: Ctx = Depends(ctx(Role.VIEWER))):
        if not c.rt.store.initialized:
            raise HTTPException(409, "Master non inizializzato")
        return c.rt.mgr.master_info()

    @app.get(P + "/master/history", response_model=list[MasterInfo], tags=["master"])
    def history(c: Ctx = Depends(ctx(Role.VIEWER))):
        return [MasterInfo(**r.__dict__) for r in reversed(c.rt.store.history())]

    @app.get(P + "/master/download", tags=["master"])
    def download(revision: Optional[int] = None, c: Ctx = Depends(ctx(Role.VIEWER))):
        try:
            data = c.rt.store.read_revision(revision)
        except RevisionPurged as exc:
            raise HTTPException(410, f"{exc}. Scarica una revisione conservata (vedi la cronologia del Master)")
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        rev = revision if revision is not None else c.rt.store.head().revision
        sha = hashlib.sha256(data).hexdigest()
        rec(c, "master_downloaded", f"rev{rev}", sha256=sha)
        st = c.rt.store
        return Response(data, media_type=media_type(st.kind), headers={
            "Content-Disposition": f'attachment; filename="{rev_filename(st.name, rev, st.ext)}"',
            "X-Revision": str(rev), "X-SHA256": sha, "X-File-Ext": st.ext, "X-File-Kind": st.kind})

    @app.post(P + "/master/restore", response_model=MasterInfo, tags=["master"])
    def restore(body: RestoreIn, c: Ctx = Depends(ctx(Role.ADMIN))):
        c.rt.mgr.restore(c.dev, body.revision, body.reason)
        return c.rt.mgr.master_info()

    @app.get(P + "/presence", response_model=list[PresenceOut], tags=["presenza"])
    def presence(c: Ctx = Depends(ctx(Role.VIEWER, use_branch=False))):
        """Chi c'e' nel progetto: per ogni collega, dove sta lavorando (principale / branch), da quale revisione e se ha in carico il Master."""
        out: dict[str, PresenceOut] = {}
        for rt in [registry.runtime(c.access.project_id)] + registry.branch_runtimes(c.access.project_id):
            holder = ""
            try:
                holder = lock_out(locks_for(rt, cfg), "").holder
            except Exception:                                    # noqa: BLE001
                pass
            for d in rt.mgr.developers():
                po = out.setdefault(d.developer, PresenceOut(username=d.developer, role=d.role))
                po.contexts.append(PresenceContext(branch=rt.branch or "", branch_name=rt.branch_name or "",
                                                   revision=d.checked_out_revision, online=d.connected,
                                                   holds_lock=bool(holder) and holder == d.developer))
                po.online = po.online or d.connected
        return sorted(out.values(), key=lambda x: (not x.online, x.username))

    @app.post(P + "/heartbeat", status_code=204, tags=["presenza"])
    def heartbeat(revision: Optional[int] = None, c: Ctx = Depends(ctx(Role.VIEWER))):
        c.rt.mgr.touch(c.dev, revision)

    @app.get(P + "/developers", response_model=list[DeveloperOut], tags=["presenza"])
    def developers(c: Ctx = Depends(ctx(Role.VIEWER))):
        return c.rt.mgr.developers()

    # ------------------------------------------------------------ proposte
    @app.post(P + "/proposals", response_model=ProposalDetail, status_code=201, tags=["merge"])
    async def submit(file: UploadFile = File(...), base_revision: int = Form(...), message: str = Form(""),
                     c: Ctx = Depends(ctx(Role.DEVELOPER))):
        data = await file.read()
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        p = await run_in_threadpool(c.rt.mgr.submit, c.dev, data, base_revision, message)
        return c.rt.mgr.detail(p)

    @app.post(P + "/proposals/dry-run", response_model=PreviewOut, tags=["merge"])
    async def dry_run(file: UploadFile = File(...), base_revision: int = Form(...), c: Ctx = Depends(ctx(Role.DEVELOPER))):
        """Anteprima del merge (conflitti inclusi) senza inviare la richiesta."""
        data = await file.read()
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "File troppo grande")
        return await run_in_threadpool(c.rt.mgr.dry_run, data, base_revision)

    @app.get(P + "/proposals", response_model=list[ProposalSummary], tags=["merge"])
    def proposals(active_only: bool = Query(False), c: Ctx = Depends(ctx(Role.VIEWER))):
        return [c.rt.mgr.summary(p) for p in c.rt.mgr.list(active_only)]

    @app.get(P + "/proposals/{prop}", response_model=ProposalDetail, tags=["merge"])
    def proposal(prop: str, c: Ctx = Depends(ctx(Role.VIEWER))):
        return c.rt.mgr.detail(c.rt.mgr.get(prop))

    @app.get(P + "/proposals/{prop}/preview", tags=["merge"])
    def preview(prop: str, c: Ctx = Depends(ctx(Role.VIEWER))):
        p = c.rt.mgr.get(prop)
        rec(c, "preview_downloaded", prop)
        return Response(p.result.merged_xml, media_type=media_type(c.rt.store.kind), headers={
            "Content-Disposition": f'attachment; filename="{c.rt.store.name}_anteprima{c.rt.store.ext}"'})

    @app.post(P + "/proposals/{prop}/resolutions", response_model=ProposalDetail, tags=["merge"])
    def resolve(prop: str, body: ResolutionsIn, c: Ctx = Depends(ctx(Role.DEVELOPER))):
        res = {cid: Resolution(r.choice, r.custom) for cid, r in body.resolutions.items()}
        return c.rt.mgr.detail(c.rt.mgr.set_resolutions(c.dev, prop, res))

    @app.post(P + "/proposals/{prop}/approve", response_model=ProposalDetail, tags=["merge"])
    def approve(prop: str, body: DecisionIn = DecisionIn(), c: Ctx = Depends(ctx(Role.DEVELOPER))):
        return c.rt.mgr.detail(c.rt.mgr.approve(c.dev, prop, body.comment))

    @app.post(P + "/proposals/{prop}/reject", response_model=ProposalDetail, tags=["merge"])
    def reject(prop: str, body: DecisionIn = DecisionIn(), c: Ctx = Depends(ctx(Role.DEVELOPER))):
        return c.rt.mgr.detail(c.rt.mgr.reject(c.dev, prop, body.comment))

    @app.post(P + "/proposals/{prop}/cancel", response_model=ProposalDetail, tags=["merge"])
    def cancel(prop: str, c: Ctx = Depends(ctx(Role.DEVELOPER))):
        return c.rt.mgr.detail(c.rt.mgr.cancel(c.dev, prop))

    @app.post(P + "/proposals/{prop}/force-merge", response_model=ProposalDetail, tags=["admin"])
    def force_merge(prop: str, body: DecisionIn, c: Ctx = Depends(ctx(Role.ADMIN))):
        return c.rt.mgr.detail(c.rt.mgr.force_merge(c.dev, prop, body.comment))

    from server.comments import mount_comments                                        # discussione sulle richieste
    mount_comments(app, ctx, registry, audit)

    app.include_router(branches_router(ctx, registry, audit, cfg))                    # branch (server/branches.py)

    # --------------------------------------------------------------- audit
    def to_audit(r) -> AuditOut:                                                     # noqa: ANN001
        try:
            det = json.loads(r.details or "{}")
        except json.JSONDecodeError:
            det = {}
        return AuditOut(id=r.id, ts=datetime.fromisoformat(r.ts), actor=r.actor, action=r.action,
                        project_id=r.project_id, target=r.target, details=det, ip=r.ip, hash=r.hash)

    @app.get("/api/audit", response_model=list[AuditOut], tags=["audit"])
    def audit_list(project_id: Optional[int] = None, action: Optional[str] = None, actor: Optional[str] = None,
                   limit: int = 200, offset: int = 0, p: Principal = Depends(min_role(Role.VIEWER))):
        """Admin: tutto (anche eventi globali). Altri: solo i progetti a cui hanno accesso."""
        with db.scope() as s:
            user = s.get(User, p.user_id)
            visible = None if p.is_admin else {
                pr.id for pr in s.query(Project).all() if effective_project_role(s, user, pr.id) is not None}
        if project_id is not None:
            if visible is not None and project_id not in visible:
                raise HTTPException(403, "Nessun accesso a questo progetto")
            visible = {project_id}
        rows = audit.query(project_ids=visible, include_global=False,
                           action=action, actor=actor, limit=limit, offset=offset)
        return [to_audit(r) for r in rows]

    @app.get("/api/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
    def audit_verify(_: Principal = Depends(min_role(Role.ADMIN))):
        ok, bad, n = audit.verify()
        return AuditVerifyOut(ok=ok, checked=n, first_bad_id=bad)

    # ------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, token: str = "", project: int = 0, branch: str = ""):
        try:
            p = await run_in_threadpool(auth.principal_from_access, token, ws.client.host if ws.client else "")
            if p.must_change_password:
                raise AuthError(403, "password")

            def access():
                with db.scope() as s:
                    u = s.get(User, p.user_id)
                    return effective_project_role(s, u, project) if s.get(Project, project) else None
            role = await run_in_threadpool(access)
            if role is None:
                raise AuthError(403, "no access")
            rt = await run_in_threadpool(registry.runtime, project, branch or None)
        except (AuthError, LookupError):
            await ws.close(code=4401)
            return
        await ws.accept()
        # chiave della connessione: principale "<pid>:u<id>", branch "<pid>:@<slug>:u<id>" (vedi server/notifier.py)
        key = f"{project}:@{branch}:u{p.user_id}" if branch else f"{project}:u{p.user_id}"
        notifier.register(key, ws)
        dev = await run_in_threadpool(rt.mgr.join, p.user_id, p.username, role)
        hello = Event(type="hello", message=f"Connesso come {p.username} ({role.label})"
                      + (f" al branch «{rt.branch_name}»" if branch else ""),
                      revision=rt.store.head().revision if rt.store.initialized else None, branch=branch or None)
        await ws.send_json(hello.model_dump(mode="json"))
        await run_in_threadpool(rt.mgr.set_ws, dev.session_id, True)
        try:
            while True:
                msg = await ws.receive_text()           # "ping" dal client = keepalive
                await run_in_threadpool(rt.mgr.set_ws, dev.session_id, True)
                if msg == "ping":
                    await ws.send_text("pong")
        except WebSocketDisconnect:
            pass
        finally:
            notifier.unregister(key, ws)
            if not any(k == key for k in notifier._conns):                           # noqa: SLF001
                await run_in_threadpool(rt.mgr.set_ws, dev.session_id, False)

    return app
