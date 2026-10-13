"""Eliminazione di un progetto (solo Admin): DB, cartella dati, audit con catena ricalcolata, WebSocket."""
import os
import sqlite3
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from server.db.audit import AuditService
from server.db.base import Database
from tests import sample_l5x as S
from tests.server_helpers import ADMIN_PW, PW, env, login, new_project, submit, tworld, world   # noqa: F401


def url(pid, name):
    return f"/api/projects/{pid}?confirm={name}"


def audit_rows(c, H, pid=None):
    q = f"?limit=1000" + (f"&project_id={pid}" if pid else "")
    return c.get("/api/audit" + q, headers=H["admin"]).json()


def test_delete_removes_everything_and_keeps_chain_valid(world):
    c, app, pid, H, users = world
    other = new_project(c, H["admin"], users, "Altro progetto")
    root = app.state.settings.data_dir / "projects"
    assert (root / "linea-1" / "revisions").is_dir() and (root / "altro-progetto").is_dir()
    before_other = [(r["id"], r["action"], r["target"]) for r in audit_rows(c, H, other)]
    n_own = len(audit_rows(c, H, pid))
    assert n_own >= 3 and before_other

    r = c.delete(url(pid, "Linea 1"), headers=H["admin"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["id"] == pid and j["name"] == "Linea 1" and j["audit_entries_removed"] == n_own and j["revisions_removed"] == 1

    assert c.get(f"/api/projects/{pid}/master", headers=H["admin"]).status_code == 404
    assert "Linea 1" not in [p["name"] for p in c.get("/api/projects", headers=H["admin"]).json()]
    assert not (root / "linea-1").exists() and (root / "altro-progetto").is_dir()
    assert not [p for p in root.iterdir() if p.name.startswith(".deleted")]
    assert app.state.registry._rt.get(pid) is None                                         # noqa: SLF001
    with app.state.db.engine.connect() as cx:                                              # permessi in cascata
        n = cx.exec_driver_sql("SELECT count(*) FROM project_permissions WHERE project_id=?", (pid,)).scalar()
        assert n == 0
        assert cx.exec_driver_sql("SELECT count(*) FROM audit_log WHERE project_id=?", (pid,)).scalar() == 0

    v = c.get("/api/audit/verify", headers=H["admin"]).json()
    assert v["ok"] and v["first_bad_id"] is None
    assert [(r["id"], r["action"], r["target"]) for r in audit_rows(c, H, other)] == before_other      # altri progetti intatti
    # nessuna traccia del progetto eliminato nell'audit (nemmeno una riga globale)
    assert not [r for r in audit_rows(c, H) if r["action"] == "project_deleted" or r["target"] == "Linea 1"
                or r["project_id"] == pid]
    with app.state.db.engine.connect() as cx:
        assert cx.exec_driver_sql("SELECT count(*) FROM audit_log WHERE details LIKE '%linea-1%'").scalar() == 0
    # l'audit continua a funzionare (catena) dopo la manutenzione
    assert c.post("/api/projects", headers=H["admin"], json={"name": "Dopo"}).status_code == 201
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"]


def test_delete_only_admin_and_requires_exact_name(world):
    c, app, pid, H, users = world
    for who in ("lead", "dev1", "viewer"):
        assert c.delete(url(pid, "Linea 1"), headers=H[who]).status_code == 403
    assert c.delete(url(pid, "Linea 1")).status_code == 401
    for bad in ("linea 1", "Linea", "Linea 1 ", ""):
        r = c.delete(f"/api/projects/{pid}", params={"confirm": bad}, headers=H["admin"])
        assert r.status_code == 422, bad
    assert c.delete(f"/api/projects/{pid}", headers=H["admin"]).status_code == 422            # senza conferma
    assert c.delete(url(9999, "x"), headers=H["admin"]).status_code == 404
    # nulla e' stato toccato
    assert (app.state.settings.data_dir / "projects" / "linea-1" / "history.json").exists()
    assert c.get(f"/api/projects/{pid}/master", headers=H["dev1"]).status_code == 200
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"]
    assert not [r for r in audit_rows(c, H) if r["action"] == "project_deleted"]


def test_delete_notifies_connected_clients_then_closes(world):
    c, app, pid, H, users = world
    tok = c.post("/api/auth/login", json={"username": "dev1", "password": PW}).json()["access_token"]
    with c.websocket_connect(f"/ws?token={tok}&project={pid}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert c.delete(url(pid, "Linea 1"), headers=H["admin"]).status_code == 200
        ev = ws.receive_json()
        while ev["type"] != "project_deleted":
            ev = ws.receive_json()
        assert "Linea 1" in ev["message"] and ev["developer"] == "admin"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert app.state.notifier.connection_count() == 0


def test_delete_with_active_proposals_and_slug_can_be_recreated(tworld):
    c, app, pid, H, users = tworld
    r = submit(c, pid, H["dev1"], b"\n".join([b"riga 0 modificata"] + [f"riga {i}".encode() for i in range(1, 10)]))
    assert r.status_code == 201, r.text
    assert c.get(f"/api/projects/{pid}/proposals?active_only=true", headers=H["admin"]).json()
    assert c.delete(url(pid, "Linea 1"), headers=H["admin"]).status_code == 200
    # stesso nome/slug: si ricrea da zero (nessun residuo del vecchio storico)
    r = c.post("/api/projects", headers=H["admin"], json={"name": "Linea 1"})
    assert r.status_code == 201 and r.json()["slug"] == "linea-1" and r.json()["head_revision"] is None
    nid = r.json()["id"]
    assert c.get(f"/api/projects/{nid}/proposals", headers=H["admin"]).json() == []
    h = app.state.settings.data_dir / "projects" / "linea-1"
    assert not (h / "history.json").exists() and not list((h / "revisions").glob("rev_*"))


def test_delete_never_follows_symlinks(world):
    c, app, pid, H, users = world
    outside = app.state.settings.data_dir.parent / "fuori_cartella_dati"
    outside.mkdir(exist_ok=True)
    (outside / "importante.txt").write_text("non toccare")
    pid2 = c.post("/api/projects", headers=H["admin"], json={"name": "Link"}).json()["id"]
    folder = app.state.settings.data_dir / "projects" / "link"
    if folder.exists():
        folder.rmdir() if not any(folder.iterdir()) else None
    try:
        os.symlink(outside, folder, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("link simbolici non disponibili")
    assert c.delete(url(pid2, "Link"), headers=H["admin"]).status_code == 200
    assert not folder.exists() and not folder.is_symlink()
    assert (outside / "importante.txt").read_text() == "non toccare"


def test_project_dir_rejects_paths_outside_projects(env):
    c, app = env
    reg = app.state.registry
    for bad in ("", ".", "..", "../x", "a/b", "/etc"):
        with pytest.raises(ValueError):
            reg.project_dir(bad)
    assert reg.project_dir("ok").parent == (app.state.settings.data_dir / "projects").resolve()


# ------------------------------------------------------------------ AuditService.purge_project
def test_purge_project_recomputes_chain_and_recreates_triggers(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    db.create_all()
    au = AuditService(db)
    for i in range(12):
        au.record(f"azione{i}", "u", project_id=1 + i % 3 if i % 4 else None, target=str(i), details={"i": i})
    n1 = sum(1 for r in au.query(project_ids=None, limit=100) if r.project_id == 2)
    assert n1 == 3 and au.verify()[0]
    assert au.purge_project(2) == 3
    ok, bad, n = au.verify()
    assert ok and bad is None and n == 9
    assert [r.project_id for r in au.query(project_ids=None, limit=100) if r.project_id == 2] == []
    assert au.purge_project(77) == 0 and au.verify()[0]
    au.record("dopo", "u")                                      # la catena prosegue
    assert au.verify() == (True, None, 10)
    # i trigger sono tornati: UPDATE/DELETE restano vietati
    with db.engine.connect() as cx:
        names = {r[0] for r in cx.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert names == {"audit_no_update", "audit_no_delete"}
    raw = sqlite3.connect(tmp_path / "a.db")
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("UPDATE audit_log SET actor='x'")
    raw.close()
    db.dispose()


def test_purge_project_rolls_back_on_error(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    db.create_all()
    au = AuditService(db)
    for i in range(4):
        au.record("x", "u", project_id=1 + i % 2)
    import server.db.audit as A

    def boom(*a, **k):
        raise RuntimeError("guasto")
    monkeypatch.setattr(A, "_digest", boom)
    with pytest.raises(RuntimeError):
        au.purge_project(1)
    monkeypatch.undo()
    assert au.verify() == (True, None, 4)                       # righe e trigger come prima
    with db.engine.connect() as cx:
        assert cx.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='trigger'").scalar() == 2
    db.dispose()
