"""Limite di revisioni conservate dal server (retention), potatura, migrazione e rollback."""
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.storage import MasterStore
from tests.server_helpers import (ADMIN_PW, PW, build_world, env, login, reimport, submit, text_master,   # noqa: F401
                                  tworld, world)


def proj(c, h, pid):
    return next(p for p in c.get("/api/projects", headers=h).json() if p["id"] == pid)


def hist(c, h, pid):
    rows = c.get(f"/api/projects/{pid}/master/history", headers=h).json()
    assert [r["revision"] for r in rows] == sorted((r["revision"] for r in rows), reverse=True)      # dalla piu' recente
    return {r["revision"]: r for r in sorted(rows, key=lambda r: r["revision"])}


def files(app, slug="linea-1"):
    d = app.state.settings.data_dir / "projects" / slug / "revisions"
    return sorted(int(p.stem.split("_")[1]) for p in d.glob("rev_*"))


def bump(c, pid, h, n):
    """Crea ``n`` nuove revisioni (reimport di un Master di testo con una riga diversa ogni volta)."""
    cur = c.get(f"/api/projects/{pid}/master", headers=h).json()["revision"]
    for i in range(n):
        r = reimport(c, pid, h, text_master(l9=f"versione {cur + i + 1}"))
        assert r.status_code == 200, r.text
    return cur + n


def patch(c, h, pid, **body):
    return c.patch(f"/api/projects/{pid}", headers=h, json=body)


def actions(c, h, pid):
    return [r for r in c.get(f"/api/audit?project_id={pid}&limit=1000", headers=h["admin"]).json()]


# ------------------------------------------------------------------------------ valori e creazione
def test_default_and_custom_limit_on_create(env):
    c, app = env
    pid, H, users = build_world(c)
    p = proj(c, H["admin"], pid)
    assert p["max_revisions"] == 10 and p["purged_revisions"] == 0
    r = c.post("/api/projects", headers=H["admin"], json={"name": "Tre", "max_revisions": 3})
    assert r.status_code == 201 and r.json()["max_revisions"] == 3 and r.json()["purged_revisions"] == 0
    assert c.post("/api/projects", headers=H["admin"], json={"name": "Uno", "max_revisions": 1}).status_code == 422
    assert c.post("/api/projects", headers=H["admin"], json={"name": "Mille", "max_revisions": 201}).status_code == 422
    app.state.settings.default_max_revisions = 7
    assert c.post("/api/projects", headers=H["admin"], json={"name": "Sette"}).json()["max_revisions"] == 7


# ------------------------------------------------------------------------------ potatura
def test_prune_after_n_plus_k_commits_never_head(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=3)
    head = bump(c, pid, H["lead"], 5)                         # rev 1..6
    assert head == 6 and files(app) == [4, 5, 6]
    h = hist(c, H["viewer"], pid)
    assert sorted(h) == [1, 2, 3, 4, 5, 6]                    # lo storico resta completo
    assert [n for n, r in h.items() if r["purged"]] == [1, 2, 3] and not h[6]["purged"]
    assert proj(c, H["viewer"], pid)["purged_revisions"] == 3
    assert c.get(f"/api/projects/{pid}/master", headers=H["viewer"]).json()["purged"] is False
    pr = [r for r in actions(c, H, pid) if r["action"] == "revisions_pruned"]
    assert sorted(x for r in pr for x in r["details"]["pruned"]) == [1, 2, 3]
    assert all(r["details"]["keep"] == 3 for r in pr)
    # download: conservata -> 200 ; potata -> 410 con messaggio chiaro
    assert c.get(f"/api/projects/{pid}/master/download?revision=4", headers=H["viewer"]).status_code == 200
    r = c.get(f"/api/projects/{pid}/master/download?revision=2", headers=H["viewer"])
    assert r.status_code == 410 and "non e' piu' conservata" in r.json()["detail"]
    assert c.get(f"/api/projects/{pid}/master/download?revision=99", headers=H["viewer"]).status_code == 404
    # sopravvive al riavvio: lo stato e' in history.json
    st = MasterStore(app.state.settings.data_dir / "projects" / "linea-1")
    assert st.purged_count() == 3 and st.is_purged(1) and not st.has_revision(1) and st.has_revision(4)


def test_active_proposal_base_is_protected_until_it_ends(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    r = submit(c, pid, H["dev1"], text_master(l0="modifica dev1"), base=1)
    assert r.status_code == 201, r.text
    prop = r.json()["id"]
    bump(c, pid, H["lead"], 4)                                # rev 5
    assert files(app) == [1, 4, 5]                            # la base 1 serve ancora al 3-way
    h = hist(c, H["viewer"], pid)
    assert [n for n, x in h.items() if x["purged"]] == [2, 3]
    assert c.get(f"/api/projects/{pid}/proposals/{prop}", headers=H["dev1"]).json()["state"] in ("awaiting_consent", "conflict")
    assert c.post(f"/api/projects/{pid}/proposals/{prop}/cancel", headers=H["dev1"]).status_code == 200
    assert files(app) == [1, 4, 5]                            # nessuna potatura finche' non c'e' una nuova revisione
    bump(c, pid, H["lead"], 1)                                # rev 6: ora la base 1 viene potata
    assert files(app) == [5, 6]


def test_merge_and_force_merge_commit_also_prune(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    bump(c, pid, H["lead"], 2)                                # rev 3, kept 2,3
    assert files(app) == [2, 3]
    prop = submit(c, pid, H["dev1"], text_master(l0="dev1", l9="versione 3"), base=3).json()["id"]
    r = c.post(f"/api/projects/{pid}/proposals/{prop}/force-merge", headers=H["admin"], json={"comment": "collaudo urgente"})
    assert r.status_code == 200 and r.json()["result_revision"] == 4, r.text
    assert files(app) == [3, 4]


def test_reduce_limit_prunes_now_and_increase_recreates_nothing(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=10)
    bump(c, pid, H["lead"], 5)                                # rev 1..6, nessuna potata
    assert files(app) == [1, 2, 3, 4, 5, 6] and proj(c, H["admin"], pid)["purged_revisions"] == 0
    tok = c.post("/api/auth/login", json={"username": "dev1", "password": PW}).json()["access_token"]
    with c.websocket_connect(f"/ws?token={tok}&project={pid}") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = patch(c, H["admin"], pid, max_revisions=3)
        assert r.status_code == 200 and r.json()["max_revisions"] == 3 and r.json()["purged_revisions"] == 3
        ev = ws.receive_json()
        while ev["type"] != "project_updated":
            ev = ws.receive_json()
        assert "linea-1" in ev["message"]
    assert files(app) == [4, 5, 6]
    acts = actions(c, H, pid)
    rc = next(a for a in acts if a["action"] == "retention_changed")
    assert rc["details"] == {"old": 10, "new": 3} and rc["actor"] == "admin"
    assert any(a["action"] == "revisions_pruned" and a["details"]["pruned"] == [1, 2, 3] for a in acts)
    r = patch(c, H["admin"], pid, max_revisions=50)           # aumentare non ricrea nulla
    assert r.status_code == 200 and r.json()["max_revisions"] == 50 and r.json()["purged_revisions"] == 3
    assert files(app) == [4, 5, 6]
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"]
    # il nuovo valore e' effettivo (e persistente) sul runtime
    assert app.state.registry.runtime(pid).mgr.max_revisions == 50
    assert patch(c, H["admin"], pid, max_revisions=50).status_code == 200      # invariato: nessun nuovo retention_changed
    assert len([a for a in actions(c, H, pid) if a["action"] == "retention_changed"]) == 2


def test_limit_validation_and_permissions(world):
    c, app, pid, H, users = world
    for bad in (0, 1, 201, -5):
        assert patch(c, H["admin"], pid, max_revisions=bad).status_code == 422, bad
    for who in ("lead", "dev1", "viewer"):
        assert patch(c, H[who], pid, max_revisions=5).status_code == 403
    assert proj(c, H["admin"], pid)["max_revisions"] == 10
    assert patch(c, H["admin"], pid, max_revisions=2).json()["max_revisions"] == 2
    assert patch(c, H["admin"], pid, max_revisions=200).json()["max_revisions"] == 200
    assert patch(c, H["admin"], pid, name="Nuovo nome").json()["max_revisions"] == 200      # altri campi invariati


# ------------------------------------------------------------------------------ basi purged / rollback
def test_submit_on_purged_base_gives_clear_409(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    bump(c, pid, H["lead"], 3)                                # rev 4: kept 3,4
    r = submit(c, pid, H["dev1"], text_master(l0="x"), base=1)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert "revisione base 1" in d and "non e' piu' conservata" in d and "ne tiene 2" in d and "Checkout" in d
    assert submit(c, pid, H["dev1"], text_master(l0="x"), base=99).status_code == 422
    assert submit(c, pid, H["dev1"], text_master(l0="x", l9="versione 4"), base=4).status_code == 201


def test_restore_works_on_kept_revisions_and_refuses_purged(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=3)
    bump(c, pid, H["lead"], 4)                                # rev 5: kept 3,4,5
    url = f"/api/projects/{pid}/master/restore"
    r = c.post(url, headers=H["admin"], json={"revision": 1, "reason": "torna indietro"})
    assert r.status_code == 422 and "non e' piu' conservata" in r.json()["detail"]
    assert c.post(url, headers=H["admin"], json={"revision": 5, "reason": "gia' corrente"}).status_code == 422
    assert c.post(url, headers=H["lead"], json={"revision": 3, "reason": "prova"}).status_code == 403
    r = c.post(url, headers=H["admin"], json={"revision": 3, "reason": "rollback collaudo"})
    assert r.status_code == 200 and r.json()["revision"] == 6
    dl = c.get(f"/api/projects/{pid}/master/download?revision=6", headers=H["admin"]).content
    assert dl == text_master(l9="versione 3")                 # contenuto della rev 3
    assert files(app) == [4, 5, 6]                            # la nuova revisione ha potato la 3
    h = hist(c, H["admin"], pid)
    assert [n for n, x in h.items() if x["purged"]] == [1, 2, 3]
    assert "Ripristino rev 3" in h[6]["message"]


# ------------------------------------------------------------------------------ storage
def test_store_prune_unit_and_history_backward_compat(tmp_path):
    st = MasterStore(tmp_path / "p")
    for i in range(6):
        st.commit(f"v{i}".encode(), "u", filename="m.txt")
    assert st.prune(3, {2}) == [1, 3]                         # restano 4,5,6 (ultime 3) + la protetta 2
    assert st.prune(3, {2}) == []                             # idempotente
    assert st.has_revision(2) and not st.has_revision(1) and st.is_purged(3) and not st.is_purged(2)
    assert st.read_revision(2) == b"v1" and st.read_revision() == b"v5"
    import pytest
    from server.storage import RevisionPurged
    with pytest.raises(RevisionPurged, match="non e' piu' conservata"):
        st.read_revision(1)
    assert st.prune(1, set()) == [2, 4, 5] and st.head().revision == 6 and st.read_revision() == b"v5"
    st.prune(0, set())                                        # la testa non si pota mai, nemmeno con keep <= 0
    assert st.has_revision(6)
    again = MasterStore(tmp_path / "p")                       # riletto da history.json
    assert [r.purged for r in again.history()] == [True] * 5 + [False]
    # history.json di versioni precedenti (senza il campo "purged")
    old = json.loads((tmp_path / "p" / "history.json").read_text())
    for r in old:
        r.pop("purged")
    (tmp_path / "p" / "history.json").write_text(json.dumps(old))
    legacy = MasterStore(tmp_path / "p")
    assert all(r.purged is False for r in legacy.history()) and legacy.head().revision == 6


# ------------------------------------------------------------------------------ migrazione
def test_old_database_without_column_is_migrated(tmp_path):
    cfg = Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW)
    app = create_app(cfg)
    with TestClient(app) as c:
        h = login(c, "admin", ADMIN_PW)
        assert c.post("/api/projects", headers=h, json={"name": "Vecchio"}).status_code == 201
    app.state.db.dispose()
    raw = sqlite3.connect(tmp_path / "aps.db")                # simula un database creato prima di questa versione
    raw.execute("DROP INDEX IF EXISTS ix_projects_slug")
    raw.execute("ALTER TABLE projects DROP COLUMN max_revisions")
    assert "max_revisions" not in [r[1] for r in raw.execute("PRAGMA table_info(projects)")]
    raw.commit()
    raw.close()
    app2 = create_app(Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW))
    with TestClient(app2) as c:
        h = login(c, "admin", ADMIN_PW)
        ps = c.get("/api/projects", headers=h).json()
        assert [p["name"] for p in ps] == ["Vecchio"] and ps[0]["max_revisions"] == 10
        assert c.patch(f"/api/projects/{ps[0]['id']}", headers=h, json={"max_revisions": 4}).json()["max_revisions"] == 4
        assert c.post("/api/projects", headers=h, json={"name": "Nuovo"}).json()["max_revisions"] == 10
    app3 = create_app(Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW))   # seconda apertura: nessun errore
    with TestClient(app3) as c:
        assert len(c.get("/api/projects", headers=login(c, "admin", ADMIN_PW)).json()) == 2


def test_seed_project_still_works(tmp_path):
    from server.__main__ import seed_project
    app = create_app(Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW))
    sample = Path(__file__).resolve().parent.parent / "samples" / "Master_rev1_base.L5X"
    pid = seed_project(app, "Demo", sample)
    with TestClient(app) as c:
        p = next(p for p in c.get("/api/projects", headers=login(c, "admin", ADMIN_PW)).json() if p["id"] == pid)
        assert p["head_revision"] == 1 and p["max_revisions"] == 10 and p["purged_revisions"] == 0
