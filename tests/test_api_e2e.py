"""Test end-to-end: JWT, RBAC, multi-progetto, flusso di merge con consenso, audit trail."""
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from server.auth.rbac import get_principal, require_role
from server.auth.models import Principal
from server.config import Settings
from server.main import create_app
from tests import sample_l5x as S

ADMIN_PW = "AdminPass123"
PW = "Password12345"


@pytest.fixture
def env(tmp_path):
    cfg = Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW, max_login_failures=5,
                   lockout_seconds=60)
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c, app


def login(c, user, pw=PW):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    j = r.json()
    return {"Authorization": f"Bearer {j['access_token']}"}, j


@pytest.fixture
def world(env):
    """Admin + progetto con Master + utenti lead/dev1/dev2/viewer (+ outsider senza permessi)."""
    c, app = env
    ha, _ = login(c, "admin", ADMIN_PW)
    users = {}
    for name, role in [("lead", "Lead_Engineer"), ("dev1", "Developer"), ("dev2", "Developer"),
                       ("viewer", "Viewer"), ("outsider", "Developer")]:
        r = c.post("/api/users", headers=ha, json={"username": name, "display_name": name.title(), "password": PW,
                                                    "role": role, "must_change_password": False})
        assert r.status_code == 201, r.text
        users[name] = r.json()["id"]
    pid = c.post("/api/projects", headers=ha, json={"name": "Linea 1"}).json()["id"]
    for name, role in [("lead", "Lead_Engineer"), ("dev1", "Developer"), ("dev2", "Developer"), ("viewer", "Viewer")]:
        r = c.put(f"/api/projects/{pid}/permissions", headers=ha, json={"user_id": users[name], "role": role})
        assert r.status_code == 200, r.text
    r = c.post(f"/api/projects/{pid}/master/init", headers=ha, files={"file": ("m.L5X", S.base_project().encode())})
    assert r.status_code == 200 and r.json()["revision"] == 1
    H = {n: login(c, n)[0] for n in users}
    H["admin"] = ha
    return c, app, pid, H, users


def online(c, pid, H, *names):
    """Rende 'attivi' (quorum del consenso) gli utenti indicati, come farebbe il client con l'heartbeat."""
    for n in names:
        assert c.post(f"/api/projects/{pid}/heartbeat", headers=H[n]).status_code == 204


def submit(c, pid, h, content, base=1, msg=""):
    return c.post(f"/api/projects/{pid}/proposals", headers=h, data={"base_revision": base, "message": msg},
                  files={"file": ("x.L5X", content.encode())})


# ======================================================================== autenticazione
def test_bootstrap_and_login_flow(env):
    c, app = env
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "sbagliata"}).status_code == 401
    h, tok = login(c, "ADMIN", ADMIN_PW)                           # username case-insensitive
    me = c.get("/api/auth/me", headers=h).json()
    assert me["username"] == "admin" and me["role"] == "Admin" and tok["expires_in"] == 900
    assert c.get("/api/auth/me", headers={"Authorization": "Bearer " + tok["access_token"][:-3] + "abc"}).status_code == 401
    assert c.get("/api/auth/me", headers={"Authorization": "Bearer " + tok["refresh_token"]}).status_code == 401   # tipo errato
    assert c.get("/api/health").json()["status"] == "ok"


def test_generated_admin_password_forces_change(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, bcrypt_rounds=4))
    pw = app.state.bootstrap_password
    assert pw and len(pw) >= 10
    with TestClient(app) as c:
        h, tok = login(c, "admin", pw)
        assert tok["user"]["must_change_password"]
        assert c.get("/api/users", headers=h).status_code == 403            # bloccato finche' non cambia password
        assert c.get("/api/auth/me", headers=h).status_code == 200
        assert c.post("/api/auth/change-password", headers=h, json={"old_password": pw, "new_password": "corta"}).status_code == 422
        assert c.post("/api/auth/change-password", headers=h, json={"old_password": "no", "new_password": "NuovaPassword1"}).status_code == 403
        assert c.post("/api/auth/change-password", headers=h, json={"old_password": pw, "new_password": "NuovaPassword1"}).status_code == 204
        assert c.get("/api/users", headers=h).status_code == 200
    app2 = create_app(Settings(data_dir=tmp_path, bcrypt_rounds=4))
    assert app2.state.bootstrap_password is None                             # nessun nuovo admin al secondo avvio


def test_lockout_after_failures(env):
    c, _ = env
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 429 and "riprova" in r.json()["detail"]


def test_refresh_rotation_reuse_detection_and_logout(env):
    c, _ = env
    h, tok = login(c, "admin", ADMIN_PW)
    r1 = c.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r1.status_code == 200 and r1.json()["refresh_token"] != tok["refresh_token"]
    # riuso del vecchio refresh token: sessione revocata (anche per il token nuovo)
    assert c.post("/api/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 401
    assert c.post("/api/auth/refresh", json={"refresh_token": r1.json()["refresh_token"]}).status_code == 401
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {r1.json()['access_token']}"}).status_code == 401
    h2, tok2 = login(c, "admin", ADMIN_PW)
    assert c.post("/api/auth/logout", headers=h2).status_code == 204
    assert c.get("/api/auth/me", headers=h2).status_code == 401


def test_sessions_listing_and_revocation(world):
    c, app, pid, H, users = world
    h2, tok2 = login(c, "dev1")
    sess = c.get("/api/auth/sessions", headers=H["dev1"]).json()
    assert len(sess) == 2 and sum(s["current"] for s in sess) == 1
    other = next(s for s in sess if not s["current"])
    assert c.delete(f"/api/auth/sessions/{other['id']}", headers=H["dev1"]).status_code == 204
    assert c.get("/api/auth/me", headers=h2).status_code == 401
    assert c.delete(f"/api/auth/sessions/{sess[0]['id']}", headers=H["dev2"]).status_code == 404   # non e' sua
    assert len(c.get("/api/auth/sessions?all=true", headers=H["admin"]).json()) >= 5


# ============================================================================== utenti / RBAC
def test_user_management_permissions_and_guards(world):
    c, app, pid, H, users = world
    body = {"username": "nuovo", "display_name": "N", "password": PW, "role": "Developer"}
    assert c.post("/api/users", headers=H["lead"], json=body).status_code == 403
    assert c.get("/api/users", headers=H["dev1"]).status_code == 403
    assert c.post("/api/users", headers=H["admin"], json={**body, "password": "debole"}).status_code == 422
    assert c.post("/api/users", headers=H["admin"], json={**body, "role": "Boss"}).status_code == 422
    assert c.post("/api/users", headers=H["admin"], json=body).status_code == 201
    assert c.post("/api/users", headers=H["admin"], json=body).status_code == 409
    me = c.get("/api/auth/me", headers=H["admin"]).json()
    assert c.patch(f"/api/users/{me['id']}", headers=H["admin"], json={"role": "Developer"}).status_code == 409
    assert c.patch(f"/api/users/{me['id']}", headers=H["admin"], json={"is_active": False}).status_code == 409
    # disattivazione: sessioni revocate e login impossibile
    assert c.patch(f"/api/users/{users['dev2']}", headers=H["admin"], json={"is_active": False}).status_code == 200
    assert c.get("/api/auth/me", headers=H["dev2"]).status_code == 401
    assert c.post("/api/auth/login", json={"username": "dev2", "password": PW}).status_code == 401
    # reset password da admin: obbliga al cambio
    c.patch(f"/api/users/{users['dev1']}", headers=H["admin"], json={"new_password": "ResetPassword9"})
    h, tok = login(c, "dev1", "ResetPassword9")
    assert tok["user"]["must_change_password"] and c.get("/api/projects", headers=h).status_code == 403


def test_project_permissions_and_effective_role(world):
    c, app, pid, H, users = world
    assert c.get("/api/projects", headers=H["outsider"]).json() == []                  # nessun permesso
    assert c.get(f"/api/projects/{pid}/master", headers=H["outsider"]).status_code == 403
    lst = {p["id"]: p for p in c.get("/api/projects", headers=H["dev1"]).json()}
    assert lst[pid]["my_role"] == "Developer" and lst[pid]["head_revision"] == 1
    assert c.get("/api/projects", headers=H["admin"]).json()[0]["my_role"] == "Admin"
    # il ruolo sul progetto non puo' superare quello dell'account
    r = c.put(f"/api/projects/{pid}/permissions", headers=H["admin"], json={"user_id": users["dev1"], "role": "Lead_Engineer"})
    assert r.status_code == 422
    r = c.put(f"/api/projects/{pid}/permissions", headers=H["admin"], json={"user_id": users["lead"], "role": "Developer"})
    perms = {p["username"]: p for p in r.json()}
    assert perms["lead"]["role"] == "Developer" and perms["lead"]["effective_role"] == "Developer"
    assert perms["lead"]["account_role"] == "Lead_Engineer" and perms["outsider"]["effective_role"] is None
    assert c.put(f"/api/projects/{pid}/permissions", headers=H["lead"], json={"user_id": users["dev1"], "role": "Viewer"}).status_code == 403
    # revoca: effetto immediato anche con token gia' emesso
    c.put(f"/api/projects/{pid}/permissions", headers=H["admin"], json={"user_id": users["viewer"], "role": None})
    assert c.get(f"/api/projects/{pid}/master", headers=H["viewer"]).status_code == 403
    assert c.post("/api/projects", headers=H["lead"], json={"name": "X"}).status_code == 403


def test_require_role_decorator_and_dependency(env):
    c, app = env
    r = APIRouter()

    @r.get("/t/lead")
    @require_role("Lead_Engineer")
    def only_lead(x: int = 1, p: Principal = Depends(get_principal)):
        return {"x": x, "who": p.username}

    app.include_router(r)
    ha, _ = login(c, "admin", ADMIN_PW)
    c.post("/api/users", headers=ha, json={"username": "dev", "display_name": "D", "password": PW,
                                           "role": "Developer", "must_change_password": False})
    c.post("/api/users", headers=ha, json={"username": "lea", "display_name": "L", "password": PW,
                                           "role": "Lead_Engineer", "must_change_password": False})
    assert c.get("/t/lead").status_code == 401
    assert c.get("/t/lead?x=5", headers=login(c, "dev")[0]).status_code == 403
    assert c.get("/t/lead?x=5", headers=login(c, "lea")[0]).json() == {"x": 5, "who": "lea"}
    assert c.get("/t/lead", headers=ha).status_code == 200                              # Admin >= Lead


# ============================================================================ workflow merge
def test_viewer_can_read_but_not_write(world):
    c, app, pid, H, users = world
    v = H["viewer"]
    assert c.get(f"/api/projects/{pid}/master", headers=v).status_code == 200
    assert c.get(f"/api/projects/{pid}/master/download", headers=v).status_code == 200
    assert c.get(f"/api/projects/{pid}/master/history", headers=v).json()[0]["revision"] == 1
    assert submit(c, pid, v, S.dev_a()).status_code == 403
    assert c.post(f"/api/projects/{pid}/master/restore", headers=v, json={"revision": 1, "reason": "xxx"}).status_code == 403


def test_full_workflow_lead_final_approval_with_ws_push(world):
    c, app, pid, H, users = world
    toks = {n: login(c, n)[1]["access_token"] for n in ("dev1", "dev2", "lead")}
    with c.websocket_connect(f"/ws?token={toks['dev2']}&project={pid}") as ws2, \
         c.websocket_connect(f"/ws?token={toks['dev1']}&project={pid}") as ws1:
        assert ws2.receive_json()["type"] == "hello" and ws1.receive_json()["type"] == "hello"
        r = submit(c, pid, H["dev1"], S.dev_a(), msg="fault + diagnostica")
        assert r.status_code == 201, r.text
        prop = r.json()
        assert prop["needs_lead_approval"] and prop["state"] == "awaiting_consent"
        ev = ws2.receive_json()
        while ev["type"] != "proposal_submitted":
            ev = ws2.receive_json()
        assert ev["proposal"]["id"] == prop["id"] and "vuole unire" in ev["message"]
        base = f"/api/projects/{pid}/proposals/{prop['id']}"
        c.post(base + "/approve", headers=H["dev1"], json={})
        d = c.post(base + "/approve", headers=H["dev2"], json={"comment": "ok"}).json()
        assert d["state"] == "awaiting_consent" and d["lead_approved_by"] is None          # manca il Lead
        d = c.post(base + "/approve", headers=H["lead"], json={}).json()
        assert d["state"] == "merged" and d["result_revision"] == 2 and d["lead_approved_by"] == "lead"
        types = set()
        while "master_updated" not in types:
            types.add(ws2.receive_json()["type"])
    assert b"XIC(Motor)OTE(Lamp);" in c.get(f"/api/projects/{pid}/master/download", headers=H["viewer"]).content
    assert [h["revision"] for h in c.get(f"/api/projects/{pid}/master/history", headers=H["viewer"]).json()] == [2, 1]


def test_force_merge_and_restore_over_http(world):
    c, app, pid, H, users = world
    online(c, pid, H, "dev1", "dev2")
    prop = submit(c, pid, H["dev1"], S.dev_a()).json()
    base = f"/api/projects/{pid}/proposals/{prop['id']}"
    assert c.post(base + "/force-merge", headers=H["lead"], json={"comment": "urgenza collaudo"}).status_code == 403
    assert c.post(base + "/force-merge", headers=H["admin"], json={"comment": "no"}).status_code == 422
    d = c.post(base + "/force-merge", headers=H["admin"], json={"comment": "urgenza collaudo"}).json()
    assert d["state"] == "merged" and d["forced_by"] == "admin"
    assert c.post(f"/api/projects/{pid}/master/restore", headers=H["lead"], json={"revision": 1, "reason": "regressione"}).status_code == 403
    r = c.post(f"/api/projects/{pid}/master/restore", headers=H["admin"], json={"revision": 1, "reason": "regressione in campo"})
    assert r.status_code == 200 and r.json()["revision"] == 3
    assert b"Lamp" not in c.get(f"/api/projects/{pid}/master/download", headers=H["admin"]).content
    assert b"Lamp" in c.get(f"/api/projects/{pid}/master/download?revision=2", headers=H["admin"]).content


def test_conflict_resolution_over_http(world):
    c, app, pid, H, users = world
    online(c, pid, H, "lead", "dev1")
    pb = submit(c, pid, H["lead"], S.dev_b(True)).json()                  # lead: nessun consenso finale richiesto
    assert not pb["needs_lead_approval"]
    for n in ("lead", "dev1"):
        c.post(f"/api/projects/{pid}/proposals/{pb['id']}/approve", headers=H[n], json={})
    assert c.get(f"/api/projects/{pid}/master", headers=H["admin"]).json()["revision"] == 2
    pa = submit(c, pid, H["dev1"], S.dev_a()).json()
    assert pa["state"] == "conflict" and pa["n_unresolved"] == 2
    base = f"/api/projects/{pid}/proposals/{pa['id']}"
    assert c.post(base + "/approve", headers=H["dev1"], json={}).status_code == 409
    d = c.get(base, headers=H["dev1"]).json()
    body = {"resolutions": {x["id"]: {"choice": "both" if x["sequence"] else "mine"} for x in d["conflicts"]}}
    assert c.post(base + "/resolutions", headers=H["dev2"], json=body).status_code == 403
    r = c.post(base + "/resolutions", headers=H["admin"], json=body)                  # Admin puo' risolvere
    assert r.status_code == 200 and r.json()["state"] == "awaiting_consent"
    assert c.get(base + "/preview", headers=H["dev2"]).status_code == 200
    assert c.post(base + "/reject", headers=H["dev2"], json={"comment": "no"}).status_code == 403     # non partecipante
    assert c.post(base + "/reject", headers=H["lead"], json={"comment": "no"}).json()["state"] == "rejected"


def test_projects_are_isolated(world):
    c, app, pid, H, users = world
    pid2 = c.post("/api/projects", headers=H["admin"], json={"name": "Linea 2"}).json()["id"]
    c.put(f"/api/projects/{pid2}/permissions", headers=H["admin"], json={"user_id": users["dev2"], "role": "Developer"})
    c.post(f"/api/projects/{pid2}/master/init", headers=H["admin"], files={"file": ("m.L5X", S.base_project().encode())})
    assert c.get(f"/api/projects/{pid2}/master", headers=H["dev1"]).status_code == 403
    assert {p["id"] for p in c.get("/api/projects", headers=H["dev2"]).json()} == {pid, pid2}
    prop = submit(c, pid2, H["dev2"], S.dev_a()).json()
    assert c.get(f"/api/projects/{pid}/proposals", headers=H["dev1"]).json() == []
    assert c.get(f"/api/projects/{pid}/proposals/{prop['id']}", headers=H["dev1"]).status_code == 404
    assert c.post(f"/api/projects/{pid2}/master/init", headers=H["admin"], files={"file": ("m", b"x")}).status_code == 409


def test_ws_rejects_unauthorized(world):
    c, app, pid, H, users = world
    tok = login(c, "outsider")[1]["access_token"]
    for url in (f"/ws?token=boh&project={pid}", f"/ws?token={tok}&project={pid}"):
        with pytest.raises(Exception):
            with c.websocket_connect(url) as ws:
                ws.receive_json()


def test_invalid_upload(world):
    c, app, pid, H, users = world
    assert submit(c, pid, H["dev1"], "<html/>").status_code == 422
    assert submit(c, pid, H["dev1"], S.base_project()).status_code == 422      # nessuna modifica
    assert submit(c, pid, H["dev1"], S.dev_a(), base=77).status_code == 422


# ================================================================================ audit trail
def test_audit_trail_content_scope_and_integrity(world):
    c, app, pid, H, users = world
    online(c, pid, H, "dev1", "dev2")
    prop = submit(c, pid, H["dev1"], S.dev_a(), msg="m").json()
    base = f"/api/projects/{pid}/proposals/{prop['id']}"
    for n in ("dev1", "dev2", "lead"):
        c.post(base + "/approve", headers=H[n], json={})
    c.get(f"/api/projects/{pid}/master/download", headers=H["viewer"])
    c.post("/api/auth/login", json={"username": "dev1", "password": "errata"})
    rows = c.get("/api/audit?limit=500", headers=H["admin"]).json()
    acts = [r["action"] for r in rows]
    for a in ("login", "login_failed", "user_created", "project_created", "master_initialized", "merge_requested",
              "consent_given", "merge_completed", "master_downloaded", "permission_granted"):
        assert a in acts, a
    done = next(r for r in rows if r["action"] == "merge_completed")
    assert done["details"]["revision"] == 2 and done["details"]["lead_approved_by"] == "lead" and done["project_id"] == pid
    dl = next(r for r in rows if r["action"] == "master_downloaded")
    assert dl["actor"] == "viewer" and dl["details"]["sha256"]
    assert not any("password" in str(r["details"]).lower() for r in rows)               # nessuna password nei log
    # visibilita': un viewer vede solo gli eventi del proprio progetto, non quelli globali (login/utenti)
    vrows = c.get("/api/audit?limit=500", headers=H["viewer"]).json()
    assert vrows and all(r["project_id"] == pid for r in vrows)
    assert c.get("/api/audit?project_id=999", headers=H["viewer"]).status_code == 403
    assert c.get("/api/audit", headers=H["outsider"]).json() == []
    assert c.get("/api/audit/verify", headers=H["viewer"]).status_code == 403
    v = c.get("/api/audit/verify", headers=H["admin"]).json()
    assert v["ok"] and v["checked"] >= len(rows) - 1
    # immutabilita': UPDATE/DELETE bloccati; manomissione aggirando il DB -> catena rotta
    with pytest.raises(sa.exc.DBAPIError):
        with app.state.db.scope() as s:
            s.execute(sa.text("UPDATE audit_log SET actor='x' WHERE id=1"))
    with pytest.raises(sa.exc.DBAPIError):
        with app.state.db.scope() as s:
            s.execute(sa.text("DELETE FROM audit_log WHERE id=1"))
    with app.state.db.engine.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_update"))
        conn.execute(sa.text("UPDATE audit_log SET actor='falso' WHERE id=3"))
    v = c.get("/api/audit/verify", headers=H["admin"]).json()
    assert not v["ok"] and v["first_bad_id"] == 3


def test_developers_listing_only_shows_users_seen(world):
    """Regressione: chi ha fatto solo chiamate HTTP (mai heartbeat/WebSocket) non deve far fallire /developers."""
    c, app, pid, H, users = world
    assert c.get(f"/api/projects/{pid}/master", headers=H["viewer"]).status_code == 200        # join senza presenza
    r = c.get(f"/api/projects/{pid}/developers", headers=H["dev2"])
    assert r.status_code == 200 and r.json() == []
    online(c, pid, H, "dev1")
    devs = c.get(f"/api/projects/{pid}/developers", headers=H["viewer"]).json()
    assert [(d["developer"], d["connected"], d["role"]) for d in devs] == [("dev1", True, "Developer")]


def test_cli_seed_project_creates_project_master_and_audit(tmp_path):
    """Regressione: `python -m server --seed-project X --master F` falliva con 'database is locked'."""
    from server.__main__ import seed_project
    master = tmp_path / "m.L5X"
    master.write_text(S.base_project(), "utf-8")
    app = create_app(Settings(data_dir=tmp_path / "d", bcrypt_rounds=4, admin_password=ADMIN_PW))
    pid = seed_project(app, "Linea Test", master)
    assert seed_project(app, "Linea Test", master) == pid                      # idempotente
    with TestClient(app) as c:
        h, _ = login(c, "admin", ADMIN_PW)
        projs = c.get("/api/projects", headers=h).json()
        assert [(p["name"], p["head_revision"]) for p in projs] == [("Linea Test", 1)]
        acts = [a["action"] for a in c.get("/api/audit", headers=h).json()]
        assert "project_created" in acts and "master_initialized" in acts
        assert c.get("/api/audit/verify", headers=h).json()["ok"] is True
