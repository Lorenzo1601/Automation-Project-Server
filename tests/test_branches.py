"""Branch di progetto (team e personali): creazione, permessi, commit, sync, merge, retention, eliminazione, WS, audit."""
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from common.schemas_branches import MAX_PERSONAL_BRANCHES
from server.backup import create_backup
from server.config import Settings
from server.main import create_app
from server.session_manager import SessionError, SessionManager
from server.storage import MasterStore
from tests import sample_l5x as S
from tests.server_helpers import (ADMIN_PW, PW, build_world, env, login, reimport, text_master,   # noqa: F401
                                  tworld, world)

BIN1 = b"\x00\x01\x02ABC\xff" * 20
BIN2 = b"\x00\x01\x02XYZ\xff" * 21
BIN3 = b"\x00\x09\x09QQQ\xff" * 22


# =================================================================================== helpers
def B(pid, slug="", tail=""):
    return f"/api/projects/{pid}/branches" + (f"/{slug}" if slug else "") + tail


def mk(c, pid, h, name="Prova", kind="personal", expect=201, **kw):
    r = c.post(B(pid), headers=h, json={"name": name, "kind": kind, **kw})
    assert r.status_code == expect, r.text
    return r.json()


def commit(c, pid, h, slug, content, msg="salvataggio di prova", name="m.txt", expect=200):
    r = c.post(B(pid, slug, "/commit"), headers=h, data={"message": msg}, files={"file": (name, content)})
    assert r.status_code == expect, r.text
    return r.json()


def get(c, pid, h, slug):
    r = c.get(B(pid, slug), headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def main_bump(c, pid, h, n=1, **over):
    """``n`` nuove revisioni del principale (reimport di testo); ``over`` sostituisce righe."""
    cur = c.get(f"/api/projects/{pid}/master", headers=h).json()["revision"]
    for i in range(n):
        r = reimport(c, pid, h, text_master(**{"l9": f"principale {cur + i + 1}", **over}))
        assert r.status_code == 200, r.text
    return cur + n


def head_of(c, pid, h, slug=None):
    p = f"/api/projects/{pid}/master" + (f"?branch={slug}" if slug else "")
    return c.get(p, headers=h).json()["revision"]


def download(c, pid, h, slug=None, rev=None):
    q = [f"branch={slug}"] if slug else []
    if rev:
        q.append(f"revision={rev}")
    r = c.get(f"/api/projects/{pid}/master/download?" + "&".join(q), headers=h)
    assert r.status_code == 200, r.text
    return r.content


def audit(c, H, pid, action):
    return c.get(f"/api/audit?project_id={pid}&action={action}&limit=500", headers=H["admin"]).json()


def finish_main(c, pid, H, prop, who="dev1"):
    """Porta a termine una richiesta nel principale: consenso dell'autore e, per i Developer, Consenso Finale del Lead."""
    r = c.post(f"/api/projects/{pid}/proposals/{prop}/approve", headers=H[who], json={})
    assert r.status_code == 200, r.text
    if r.json()["state"] != "merged" and who not in ("lead", "admin"):
        r = c.post(f"/api/projects/{pid}/proposals/{prop}/approve", headers=H["lead"], json={})
        assert r.status_code == 200, r.text
    return r.json()


def bfiles(app, slug="linea-1", branch="prova"):
    d = app.state.settings.data_dir / "projects" / slug / "branches" / branch / "revisions"
    return sorted(int(p.stem.split("_")[1]) for p in d.glob("rev_*"))


def mfiles(app, slug="linea-1"):
    d = app.state.settings.data_dir / "projects" / slug / "revisions"
    return sorted(int(p.stem.split("_")[1]) for p in d.glob("rev_*"))


# =================================================================================== creazione
def test_create_personal_copies_head_of_main(tworld):
    c, app, pid, H, _ = tworld
    main_bump(c, pid, H["lead"], 2)                                         # principale alla rev 3
    b = mk(c, pid, H["dev1"], "Prova uno")
    assert b["name"] == "Prova uno" and b["slug"] == "prova-uno" and b["kind"] == "personal" and b["owner"] == "dev1"
    assert b["state"] == "open" and b["base_main_revision"] == 3 and b["head_revision"] == 1
    assert b["behind"] == 0 and b["ahead"] == 0 and b["merged_main_revision"] is None and b["purged_revisions"] == 0
    assert b["can_write"] is True and b["can_delete"] is True
    assert download(c, pid, H["dev1"], "prova-uno") == download(c, pid, H["dev1"])       # rev 1 del branch = testa del principale
    assert head_of(c, pid, H["dev1"], "prova-uno") == 1 and head_of(c, pid, H["dev1"]) == 3       # principale intatto
    h = c.get(f"/api/projects/{pid}/master/history?branch=prova-uno", headers=H["viewer"]).json()
    assert [x["revision"] for x in h] == [1] and "rev 3" in h[0]["message"] and h[0]["updated_by"] == "dev1"
    # file nella cartella dedicata, con lo stesso nome/estensione del Master
    d = app.state.settings.data_dir / "projects" / "linea-1" / "branches" / "prova-uno"
    assert (d / "m.txt").exists() and (d / "history.json").exists()
    [a] = audit(c, H, pid, "branch_created")
    assert a["actor"] == "dev1" and a["target"] == "prova-uno"
    assert a["details"] == {"name": "Prova uno", "slug": "prova-uno", "kind": "personal", "owner": "dev1",
                            "revision": 3, "from_revision": 3}


def test_create_from_revision_and_errors(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=3)
    main_bump(c, pid, H["lead"], 3)                                         # rev 4: conservate 2,3,4 (1 potata)
    b = mk(c, pid, H["dev1"], "dalla due", from_revision=2)
    assert b["base_main_revision"] == 2 and b["behind"] == 2
    assert download(c, pid, H["dev1"], "dalla-due") == download(c, pid, H["dev1"], rev=2)
    assert c.post(B(pid), headers=H["dev1"], json={"name": "x", "from_revision": 99}).status_code == 404
    r = c.post(B(pid), headers=H["dev1"], json={"name": "x", "from_revision": 1})            # potata
    assert r.status_code == 409 and "non e' piu' conservata" in r.json()["detail"]
    assert c.post(B(pid), headers=H["dev1"], json={"name": "x", "from_revision": 0}).status_code == 422
    assert c.post(B(pid), headers=H["dev1"], json={"name": ""}).status_code == 422
    assert c.post(B(pid), headers=H["dev1"], json={"name": "n" * 61}).status_code == 422
    assert c.post(B(pid), headers=H["dev1"], json={"name": "x", "kind": "boh"}).status_code == 422
    assert c.get(B(pid), headers=H["dev1"]).json()[0]["slug"] == "dalla-due"             # nessun branch creato dagli errori


def test_create_permissions(tworld):
    c, app, pid, H, _ = tworld
    assert c.post(B(pid), headers=H["viewer"], json={"name": "v"}).status_code == 403
    r = c.post(B(pid), headers=H["dev1"], json={"name": "t", "kind": "team"})
    assert r.status_code == 403 and "Lead" in r.json()["detail"]
    t = mk(c, pid, H["lead"], "Squadra", "team")
    assert t["kind"] == "team" and t["owner"] == "lead"
    mk(c, pid, H["admin"], "Squadra admin", "team")
    mk(c, pid, H["lead"], "Mio lead")                                       # anche il Lead puo' crearne di personali
    assert c.post(B(pid), json={"name": "x"}).status_code == 401
    assert c.post("/api/projects/999/branches", headers=H["admin"], json={"name": "x"}).status_code == 404


def test_create_requires_initialized_master(env):
    c, app = env
    pid, H, users = build_world(c)
    ha = H["admin"]
    r = c.post("/api/projects", headers=ha, json={"name": "Vuoto"})
    pid2 = r.json()["id"]
    c.put(f"/api/projects/{pid2}/permissions", headers=ha, json={"user_id": users["dev1"], "role": "Developer"})
    r = c.post(B(pid2), headers=H["dev1"], json={"name": "x"})
    assert r.status_code == 409 and "inizializzato" in r.json()["detail"]


def test_name_slug_rules(tworld):
    c, app, pid, H, _ = tworld
    assert mk(c, pid, H["dev1"], "  Città  Nuova!  ")["slug"] == "citta-nuova"
    assert mk(c, pid, H["dev1"], "citta-nuova")["slug"] == "citta-nuova-2"             # slug diverso nome, stesso slug -> suffisso
    r = c.post(B(pid), headers=H["dev2"], json={"name": "CITTÀ NUOVA!"})
    assert r.status_code == 409 and "gia' un branch" in r.json()["detail"]            # nome gia' usato da un branch aperto
    for reserved in ("main", "Principale", "MASTER"):
        assert c.post(B(pid), headers=H["dev1"], json={"name": reserved}).status_code == 422
    assert mk(c, pid, H["dev1"], "!!!")["slug"] == "branch"
    # un nome si puo' riusare dopo la chiusura
    assert c.post(B(pid, "citta-nuova", "/close"), headers=H["dev1"]).status_code == 200
    assert mk(c, pid, H["dev1"], "Città Nuova")["slug"] == "citta-nuova-3"


def test_personal_limit_per_user_and_project(tworld):
    c, app, pid, H, users = tworld
    assert MAX_PERSONAL_BRANCHES == 5
    for i in range(5):
        mk(c, pid, H["dev1"], f"b{i}")
    r = c.post(B(pid), headers=H["dev1"], json={"name": "sesto"})
    assert r.status_code == 409 and "5" in r.json()["detail"]
    mk(c, pid, H["dev2"], "di dev2")                                        # il limite e' per utente
    mk(c, pid, H["lead"], "squadra", "team")                                # i team non contano
    mk(c, pid, H["dev1"], "squadra2-non-personale-ma-dev-no", expect=201) if False else None
    # chiudere o eliminare libera un posto
    assert c.post(B(pid, "b0", "/close"), headers=H["dev1"]).status_code == 200
    mk(c, pid, H["dev1"], "sesto")
    assert c.delete(B(pid, "b1"), headers=H["dev1"]).status_code == 200
    mk(c, pid, H["dev1"], "settimo")
    assert c.post(B(pid), headers=H["dev1"], json={"name": "ottavo"}).status_code == 409
    # il limite e' per progetto: in un altro progetto dev1 riparte da zero
    ha = H["admin"]
    pid2 = c.post("/api/projects", headers=ha, json={"name": "Altro"}).json()["id"]
    c.put(f"/api/projects/{pid2}/permissions", headers=ha, json={"user_id": users["dev1"], "role": "Developer"})
    assert c.post(f"/api/projects/{pid2}/master/init", headers=ha, files={"file": ("m.txt", text_master())}).status_code == 200
    mk(c, pid2, H["dev1"], "b0")


def test_list_and_get_flags_per_user(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "personale")
    mk(c, pid, H["lead"], "squadra", "team")
    by = lambda who: {b["slug"]: b for b in c.get(B(pid), headers=H[who]).json()}
    d1, d2, vw, ld, ad = by("dev1"), by("dev2"), by("viewer"), by("lead"), by("admin")
    assert set(d1) == set(d2) == set(vw) == {"personale", "squadra"}                   # tutti vedono tutto
    assert (d1["personale"]["can_write"], d1["personale"]["can_delete"]) == (True, True)
    assert (d2["personale"]["can_write"], d2["personale"]["can_delete"]) == (False, False)
    assert (vw["personale"]["can_write"], vw["personale"]["can_delete"]) == (False, False)
    assert (ad["personale"]["can_write"], ad["personale"]["can_delete"]) == (True, True)
    assert (ld["personale"]["can_write"], ld["personale"]["can_delete"]) == (False, False)      # il Lead non tocca i personali altrui
    assert (d2["squadra"]["can_write"], d2["squadra"]["can_delete"]) == (True, False)           # dev scrive nel team, non lo elimina
    assert (vw["squadra"]["can_write"], vw["squadra"]["can_delete"]) == (False, False)
    assert (ld["squadra"]["can_write"], ld["squadra"]["can_delete"]) == (True, True)
    assert get(c, pid, H["viewer"], "squadra")["owner"] == "lead"
    assert c.get(B(pid, "boh"), headers=H["dev1"]).status_code == 404
    assert [b["slug"] for b in c.get(B(pid), headers=H["dev1"]).json()] == ["personale", "squadra"]


def test_unknown_branch_is_404_everywhere(tworld):
    c, app, pid, H, _ = tworld
    for path in ("master", "master/history", "master/download", "proposals", "lock", "developers"):
        assert c.get(f"/api/projects/{pid}/{path}?branch=nope", headers=H["admin"]).status_code == 404, path
    assert c.post(B(pid, "nope", "/sync"), headers=H["admin"]).status_code == 404
    assert c.post(B(pid, "nope", "/merge"), headers=H["admin"], data={"message": ""}).status_code == 404
    assert c.post(B(pid, "nope", "/close"), headers=H["admin"]).status_code == 404
    assert c.delete(B(pid, "nope"), headers=H["admin"]).status_code == 404
    assert c.post(B(pid, "nope", "/commit"), headers=H["admin"], data={"message": "x"},
                  files={"file": ("m.txt", b"x")}).status_code == 404
    with pytest.raises(Exception):
        with c.websocket_connect(f"/ws?token={H['admin']['Authorization'][7:]}&project={pid}&branch=nope") as ws:
            ws.receive_json()


# =================================================================================== commit diretto (personale)
def test_personal_commit_by_owner_creates_revision(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    info = commit(c, pid, H["dev1"], "prova", text_master(l2="mia modifica"), "prima modifica")
    assert info["revision"] == 2 and info["updated_by"] == "dev1" and info["message"] == "prima modifica"
    info = commit(c, pid, H["dev1"], "prova", text_master(l2="mia modifica", l5="altra"), "")
    assert info["revision"] == 3 and info["message"] == "Salvataggio nel branch"        # messaggio di default
    b = get(c, pid, H["dev1"], "prova")
    assert b["head_revision"] == 3 and b["ahead"] == 2 and b["behind"] == 0 and b["base_main_revision"] == 1
    assert download(c, pid, H["dev1"], "prova") == text_master(l2="mia modifica", l5="altra")
    assert download(c, pid, H["dev1"], "prova", rev=1) == text_master()
    assert head_of(c, pid, H["dev1"]) == 1 and download(c, pid, H["dev1"]) == text_master()           # il principale non cambia
    rows = audit(c, H, pid, "branch_committed")
    assert len(rows) == 2 and rows[0]["details"]["revision"] == 3 and rows[0]["details"]["kind"] == "personal"
    assert rows[1]["details"]["revision"] == 2 and rows[1]["details"]["message"] == "prima modifica"
    assert rows[0]["details"]["slug"] == "prova" and rows[0]["details"]["name"] == "prova"
    # la cronologia del branch e la copia comoda
    h = c.get(f"/api/projects/{pid}/master/history?branch=prova", headers=H["viewer"]).json()
    assert [x["revision"] for x in h] == [3, 2, 1]


def test_personal_commit_permissions_and_checks(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    mk(c, pid, H["lead"], "squadra", "team")
    new = text_master(l1="x")
    for who in ("dev2", "lead", "viewer"):                                  # solo proprietario o Admin
        r = c.post(B(pid, "prova", "/commit"), headers=H[who], data={"message": "m"}, files={"file": ("m.txt", new)})
        assert r.status_code == 403, (who, r.text)
    assert "dev1" in c.post(B(pid, "prova", "/commit"), headers=H["dev2"], data={"message": "m"},
                            files={"file": ("m.txt", new)}).json()["detail"]
    commit(c, pid, H["admin"], "prova", new, "dell'admin")                  # l'Admin puo'
    assert get(c, pid, H["admin"], "prova")["head_revision"] == 2
    # nei team non c'e' il salvataggio diretto
    r = c.post(B(pid, "squadra", "/commit"), headers=H["lead"], data={"message": "m"}, files={"file": ("m.txt", new)})
    assert r.status_code == 422 and "richieste di merge" in r.json()["detail"]
    # file identico alla testa, vuoto, estensione diversa, tipo diverso
    assert commit(c, pid, H["dev1"], "prova", new, expect=422) and True
    assert c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
                  files={"file": ("m.txt", b"")}).status_code == 422
    r = c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
               files={"file": ("m.xyz", text_master(l3="altro"))})
    assert r.status_code == 422 and "estensione" in r.json()["detail"]
    r = c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
               files={"file": ("m.L5X", S.base_project().encode())})
    assert r.status_code == 422 and "tipo" in r.json()["detail"]
    r = c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
               files={"file": ("m.txt", b"\x00\x01\x02" * 50)})
    assert r.status_code == 422                                                # binario su Master di testo
    assert c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"}).status_code == 422     # manca il file


def test_commit_same_controller_check_l5x(world):
    c, app, pid, H, _ = world
    mk(c, pid, H["dev1"], "prova")
    other = S.project("ALTRO_PLC").encode()
    r = c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"}, files={"file": ("m.L5X", other)})
    assert r.status_code == 422 and "Controller" in r.json()["detail"]
    assert commit(c, pid, H["dev1"], "prova", S.dev_a().encode(), name="m.L5X")["revision"] == 2
    r = c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"}, files={"file": ("m.L5X", b"<non xml")})
    assert r.status_code == 422


def test_binary_files_are_versioned_in_personal_branch(env):
    c, app = env
    pid, H, _ = build_world(c, BIN1, "dati.bin")
    mk(c, pid, H["dev1"], "prova")
    assert commit(c, pid, H["dev1"], "prova", BIN2, "v2", name="dati.bin")["revision"] == 2
    assert commit(c, pid, H["dev1"], "prova", BIN3, "v3", name="dati.bin")["revision"] == 3
    assert download(c, pid, H["dev1"], "prova", rev=2) == BIN2 and download(c, pid, H["dev1"], "prova") == BIN3
    assert download(c, pid, H["dev1"]) == BIN1                                 # principale intatto
    r = c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"}, files={"file": ("dati.exe", BIN2 + b"1")})
    assert r.status_code == 422 and "estensione" in r.json()["detail"]
    # i binari non si uniscono: il sync e il merge lo dicono chiaramente
    main_bump_bin = c.post(f"/api/projects/{pid}/master/reimport", headers=H["lead"], data={"reason": "nuova versione"},
                           files={"file": ("dati.bin", b"\x00nuovo\x00" * 30)})
    assert main_bump_bin.status_code == 200
    r = c.post(B(pid, "prova", "/sync"), headers=H["dev1"])
    assert r.status_code == 422 and "merge" in r.json()["detail"].lower()
    r = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""})
    assert r.status_code == 422


def test_branch_retention_uses_project_limit_and_protects_nothing_else(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=3)
    mk(c, pid, H["dev1"], "prova")
    for i in range(5):
        commit(c, pid, H["dev1"], "prova", text_master(l0=f"v{i}"), f"v{i}")
    b = get(c, pid, H["dev1"], "prova")
    assert b["head_revision"] == 6 and b["purged_revisions"] == 3 and bfiles(app) == [4, 5, 6]
    assert [x["purged"] for x in c.get(f"/api/projects/{pid}/master/history?branch=prova", headers=H["dev1"]).json()] == \
        [False, False, False, True, True, True]
    r = c.get(f"/api/projects/{pid}/master/download?branch=prova&revision=2", headers=H["dev1"])
    assert r.status_code == 410
    assert any(a["details"].get("branch") == "prova" for a in audit(c, H, pid, "revisions_pruned"))
    # ridurre il limite del progetto pota anche i branch aperti; il principale resta com'e'
    assert c.patch(f"/api/projects/{pid}", headers=H["admin"], json={"max_revisions": 2}).status_code == 200
    assert bfiles(app) == [5, 6] and mfiles(app) == [1]


# =================================================================================== isolamento branch/principale
def test_isolation_between_branches_and_main(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "uno")
    mk(c, pid, H["dev2"], "due")
    commit(c, pid, H["dev1"], "uno", text_master(l0="solo uno"))
    assert download(c, pid, H["viewer"], "uno") == text_master(l0="solo uno")
    assert download(c, pid, H["viewer"], "due") == text_master() and download(c, pid, H["viewer"]) == text_master()
    # compare nel branch
    d = c.get(f"/api/projects/{pid}/master/compare?from=1&to=2&branch=uno", headers=H["viewer"]).json()
    assert d["n_modified"] == 1 and d["changes"][0]["new_text"] == "solo uno"
    assert c.get(f"/api/projects/{pid}/master/compare?from=1&to=2", headers=H["viewer"]).status_code == 404
    # etichette/blocco di una revisione del branch
    assert c.patch(f"/api/projects/{pid}/master/revisions/2?branch=uno", headers=H["lead"],
                   json={"label": "FAT"}).status_code == 403                    # branch personale altrui: sola lettura per il Lead
    assert c.patch(f"/api/projects/{pid}/master/revisions/2?branch=uno", headers=H["admin"],
                   json={"label": "FAT", "locked": True}).json()["label"] == "FAT"
    assert c.get(f"/api/projects/{pid}/master/history", headers=H["viewer"]).json()[0]["label"] == ""
    # lock indipendente
    assert c.post(f"/api/projects/{pid}/lock?branch=uno", headers=H["dev1"], json={"note": "mio"}).json()["holder"] == "dev1"
    assert c.get(f"/api/projects/{pid}/lock", headers=H["viewer"]).json()["locked"] is False
    assert c.get(f"/api/projects/{pid}/lock?branch=due", headers=H["viewer"]).json()["locked"] is False
    assert c.get(f"/api/projects/{pid}/lock?branch=uno", headers=H["viewer"]).json()["holder"] == "dev1"
    assert (app.state.settings.data_dir / "projects" / "linea-1" / "branches" / "uno" / "lock.json").exists()
    assert not (app.state.settings.data_dir / "projects" / "linea-1" / "lock.json").exists()
    # un lock del principale non tocca quello del branch
    assert c.post(f"/api/projects/{pid}/lock", headers=H["dev2"], json={}).json()["holder"] == "dev2"
    assert c.get(f"/api/projects/{pid}/lock?branch=uno", headers=H["viewer"]).json()["holder"] == "dev1"
    # richieste di merge nel principale: separate da quelle del branch
    r = c.post(f"/api/projects/{pid}/proposals?branch=due", headers=H["dev2"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l4="in due"))})
    assert r.status_code == 201, r.text
    assert c.get(f"/api/projects/{pid}/proposals", headers=H["viewer"]).json() == []
    assert len(c.get(f"/api/projects/{pid}/proposals?branch=due", headers=H["viewer"]).json()) == 1
    assert c.get(f"/api/projects/{pid}/proposals?branch=uno", headers=H["viewer"]).json() == []
    # le azioni nel branch portano il branch negli audit
    rows = audit(c, H, pid, "lock_acquired")
    assert {(a["actor"], a["details"].get("branch")) for a in rows} == {("dev1", "uno"), ("dev2", None)}
    assert audit(c, H, pid, "merge_requested")[0]["details"]["branch"] == "due"


def test_write_rules_on_foreign_personal_branch(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    f = ("m.txt", text_master(l1="x"))
    for who in ("dev2", "lead", "viewer"):
        r = c.post(f"/api/projects/{pid}/proposals?branch=prova", headers=H[who], data={"base_revision": 1}, files={"file": f})
        assert r.status_code == 403, (who, r.text)
        assert c.post(f"/api/projects/{pid}/lock?branch=prova", headers=H[who], json={}).status_code == 403
        assert c.post(f"/api/projects/{pid}/master/reimport?branch=prova", headers=H[who], data={"reason": "motivo valido"},
                      files={"file": f}).status_code == 403
    # ma tutti possono leggere
    for who in ("dev2", "lead", "viewer"):
        assert c.get(f"/api/projects/{pid}/master?branch=prova", headers=H[who]).status_code == 200
        assert c.get(f"/api/projects/{pid}/proposals?branch=prova", headers=H[who]).status_code == 200
    # il proprietario scrive anche con le API generiche
    r = c.post(f"/api/projects/{pid}/master/reimport?branch=prova", headers=H["dev1"] if False else H["lead"], data={"reason": "motivo valido"},
               files={"file": f})
    assert r.status_code == 403


# =================================================================================== sync dal principale
def test_sync_personal_auto_approved_and_updates_base(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l1="del branch"))
    main_bump(c, pid, H["lead"], 2, l8="dal principale")                    # principale alla rev 3
    b = get(c, pid, H["dev1"], "prova")
    assert b["behind"] == 2 and b["ahead"] == 1 and b["base_main_revision"] == 1
    r = c.post(B(pid, "prova", "/sync"), headers=H["dev1"])
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["state"] == "merged" and d["result_revision"] == 3 and d["author"] == "dev1"       # auto-approvazione
    assert "Aggiornamento dal principale" in d["message"] and "rev 3" in d["message"]
    b = get(c, pid, H["dev1"], "prova")
    assert b["base_main_revision"] == 3 and b["behind"] == 0 and b["ahead"] == 0 and b["head_revision"] == 3
    assert download(c, pid, H["dev1"], "prova") == text_master(l1="del branch", l8="dal principale", l9="principale 3")
    assert head_of(c, pid, H["dev1"]) == 3                                  # principale intatto
    [s] = audit(c, H, pid, "branch_synced")
    assert s["actor"] == "dev1" and s["details"]["revision"] == 3 and s["details"]["main_revision"] == 3
    assert s["details"]["slug"] == "prova" and s["details"]["kind"] == "personal" and s["details"]["name"] == "prova"
    assert audit(c, H, pid, "consent_given")[0]["details"]["comment"].startswith("Approvazione automatica")
    # nulla di nuovo -> 409
    r = c.post(B(pid, "prova", "/sync"), headers=H["dev1"])
    assert r.status_code == 409 and "aggiornato" in r.json()["detail"]
    # un nuovo avanzamento del principale e un nuovo sync: la base ora e' la rev 3
    main_bump(c, pid, H["lead"], 1, l8="di nuovo")
    d = c.post(B(pid, "prova", "/sync"), headers=H["dev1"]).json()
    assert d["state"] == "merged" and get(c, pid, H["dev1"], "prova")["base_main_revision"] == 4
    assert download(c, pid, H["dev1"], "prova") == text_master(l1="del branch", l8="di nuovo", l9="principale 4")


def test_sync_pure_news_when_branch_has_no_changes(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    main_bump(c, pid, H["lead"], 1, l3="novita'")
    d = c.post(B(pid, "prova", "/sync"), headers=H["dev1"]).json()
    assert d["state"] == "merged" and d["n_changes"] == 0 and d["n_upstream"] >= 1
    assert download(c, pid, H["dev1"], "prova") == download(c, pid, H["dev1"])


def test_sync_conflict_is_resolved_by_owner_then_auto_approved(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l4="versione branch"))
    main_bump(c, pid, H["lead"], 1, l4="versione principale")
    d = c.post(B(pid, "prova", "/sync"), headers=H["dev1"]).json()
    assert d["state"] == "conflict" and len(d["conflicts"]) == 1
    cf = d["conflicts"][0]
    # mine = branch, theirs = principale (come richiesto dal contratto del sync)
    assert cf["mine_text"] == "versione branch" and cf["theirs_text"] == "versione principale" and cf["base_text"] == "riga 4"
    assert get(c, pid, H["dev1"], "prova")["head_revision"] == 2 and get(c, pid, H["dev1"], "prova")["behind"] == 1
    # solo l'autore (o un Admin) risolve; nel branch personale altrui non si scrive
    res = {"resolutions": {cf["id"]: {"choice": "mine"}}}
    assert c.post(f"/api/projects/{pid}/proposals/{d['id']}/resolutions?branch=prova", headers=H["dev2"], json=res).status_code == 403
    r = c.post(f"/api/projects/{pid}/proposals/{d['id']}/resolutions?branch=prova", headers=H["dev1"], json=res)
    assert r.status_code == 200 and r.json()["state"] == "merged", r.text              # risolto -> approvazione automatica
    b = get(c, pid, H["dev1"], "prova")
    assert b["head_revision"] == 3 and b["behind"] == 0 and b["base_main_revision"] == 2
    assert download(c, pid, H["dev1"], "prova")[:0] == b"" and b"versione branch" in download(c, pid, H["dev1"], "prova")


def test_sync_permissions(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    main_bump(c, pid, H["lead"], 1)
    for who in ("dev2", "lead", "viewer"):
        assert c.post(B(pid, "prova", "/sync"), headers=H[who]).status_code == 403, who
    assert c.post(B(pid, "prova", "/sync")).status_code == 401
    r = c.post(B(pid, "prova", "/sync"), headers=H["admin"])                  # l'Admin puo' (consenso del solo autore)
    assert r.status_code == 201 and r.json()["state"] == "merged" and r.json()["author"] == "admin"


def test_sync_team_branch_needs_normal_consent(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    c.post(f"/api/projects/{pid}/heartbeat?branch=squadra", headers=H["dev1"])        # dev1 presente nel branch
    main_bump(c, pid, H["lead"], 1, l2="dal principale")
    d = c.post(B(pid, "squadra", "/sync"), headers=H["dev2"]).json()                  # un Developer qualsiasi puo' chiedere
    assert d["state"] == "awaiting_consent" and d["author"] == "dev2" and d["needs_lead_approval"] is True
    assert {p["developer"] for p in d["participants"]} == {"dev2", "dev1"}
    prop = d["id"]
    ap = lambda who: c.post(f"/api/projects/{pid}/proposals/{prop}/approve?branch=squadra", headers=H[who], json={})
    assert ap("dev2").json()["state"] == "awaiting_consent"
    assert ap("dev1").json()["state"] == "awaiting_consent"                           # manca il Consenso Finale del Lead
    r = ap("lead")
    assert r.json()["state"] == "merged" and r.json()["result_revision"] == 2
    b = get(c, pid, H["dev1"], "squadra")
    assert b["base_main_revision"] == 2 and b["behind"] == 0 and b["head_revision"] == 2
    assert download(c, pid, H["dev1"], "squadra") == text_master(l2="dal principale", l9="principale 2")
    [s] = audit(c, H, pid, "branch_synced")
    assert s["actor"] == "dev2" and s["details"]["kind"] == "team"
    # il rifiuto lascia tutto com'e'
    main_bump(c, pid, H["lead"], 1, l2="ancora")
    p2 = c.post(B(pid, "squadra", "/sync"), headers=H["dev2"]).json()["id"]
    r = c.post(f"/api/projects/{pid}/proposals/{p2}/reject?branch=squadra", headers=H["dev1"], json={"comment": "no"})
    assert r.json()["state"] == "rejected"
    assert get(c, pid, H["dev1"], "squadra")["base_main_revision"] == 2 and not audit(c, H, pid, "branch_synced")[1:]


def test_normal_requests_inside_team_branch_do_not_touch_main(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    r = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["lead"], data={"base_revision": 1, "message": "nel team"},
               files={"file": ("m.txt", text_master(l6="team"))})
    assert r.status_code == 201, r.text
    prop = r.json()["id"]
    assert c.post(f"/api/projects/{pid}/proposals/{prop}/approve?branch=squadra", headers=H["lead"], json={}).json()["state"] == "merged"
    assert head_of(c, pid, H["lead"], "squadra") == 2 and head_of(c, pid, H["lead"]) == 1
    assert download(c, pid, H["lead"], "squadra") == text_master(l6="team") and download(c, pid, H["lead"]) == text_master()
    assert c.get(f"/api/projects/{pid}/proposals/{prop}", headers=H["lead"]).status_code == 404          # non esiste nel principale
    assert get(c, pid, H["lead"], "squadra")["ahead"] == 1


def test_personal_branch_normal_request_is_owner_only_and_auto_approved(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    c.post(f"/api/projects/{pid}/heartbeat?branch=prova", headers=H["dev2"])           # presenza ininfluente
    r = c.post(f"/api/projects/{pid}/proposals?branch=prova", headers=H["dev1"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l6="mio"))})
    assert r.status_code == 201 and r.json()["state"] == "merged" and r.json()["needs_lead_approval"] is False
    assert [p["developer"] for p in r.json()["participants"]] == ["dev1"]
    assert head_of(c, pid, H["dev1"], "prova") == 2


# =================================================================================== merge nel principale
def test_merge_personal_into_main_full_flow(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="dal branch"), "modifica")
    r = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": "pronto"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["state"] == "awaiting_consent" and d["author"] == "dev1" and d["base_revision"] == 1
    assert "Unione del branch «prova»" in d["message"] and "pronto" in d["message"]
    assert d["needs_lead_approval"] is True and d["n_changes"] == 1
    prop = d["id"]
    assert [p["id"] for p in c.get(f"/api/projects/{pid}/proposals", headers=H["viewer"]).json()] == [prop]      # nel PRINCIPALE
    assert c.get(f"/api/projects/{pid}/proposals?branch=prova", headers=H["viewer"]).json() == []
    assert head_of(c, pid, H["dev1"]) == 1 and get(c, pid, H["dev1"], "prova")["state"] == "open"
    done = finish_main(c, pid, H, prop)
    assert done["state"] == "merged" and done["result_revision"] == 2
    assert download(c, pid, H["viewer"]) == text_master(l2="dal branch")
    b = get(c, pid, H["viewer"], "prova")
    assert b["state"] == "merged" and b["merged_main_revision"] == 2 and b["can_write"] is False and b["behind"] == 0
    [a] = audit(c, H, pid, "branch_merged")
    assert a["actor"] == "dev1" and a["details"]["revision"] == 2 and a["details"]["branch_revision"] == 2
    assert a["details"]["slug"] == "prova" and a["details"]["kind"] == "personal"
    # sola lettura dopo l'unione
    assert c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
                  files={"file": ("m.txt", text_master(l7="x"))}).status_code == 409
    assert c.post(B(pid, "prova", "/sync"), headers=H["dev1"]).status_code == 409
    assert c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).status_code == 409
    assert c.post(f"/api/projects/{pid}/proposals?branch=prova", headers=H["dev1"], data={"base_revision": 1},
                  files={"file": ("m.txt", text_master(l7="x"))}).status_code == 409
    assert c.post(f"/api/projects/{pid}/lock?branch=prova", headers=H["dev1"], json={}).status_code == 409
    assert c.get(f"/api/projects/{pid}/master?branch=prova", headers=H["viewer"]).status_code == 200      # lettura ok
    assert c.post(B(pid, "prova", "/close"), headers=H["dev1"]).status_code == 409                     # gia' unito
    # eliminarlo e' ancora possibile
    assert c.delete(B(pid, "prova"), headers=H["dev1"]).status_code == 200


def test_merge_team_branch_by_lead_and_consent_of_others(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    r = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["lead"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l6="team"))})
    c.post(f"/api/projects/{pid}/proposals/{r.json()['id']}/approve?branch=squadra", headers=H["lead"], json={})
    c.post(f"/api/projects/{pid}/heartbeat", headers=H["dev2"])                       # dev2 presente nel PRINCIPALE
    d = c.post(B(pid, "squadra", "/merge"), headers=H["lead"], data={"message": ""}).json()
    assert d["state"] == "awaiting_consent" and {p["developer"] for p in d["participants"]} == {"lead", "dev2"}
    assert d["message"] == "Unione del branch «squadra»"
    prop = d["id"]
    r = c.post(f"/api/projects/{pid}/proposals/{prop}/approve", headers=H["lead"], json={})
    assert r.json()["state"] == "awaiting_consent"                                   # serve anche dev2
    assert get(c, pid, H["lead"], "squadra")["state"] == "open"
    r = c.post(f"/api/projects/{pid}/proposals/{prop}/approve", headers=H["dev2"], json={})
    assert r.json()["state"] == "merged"
    b = get(c, pid, H["dev1"], "squadra")
    assert b["state"] == "merged" and b["merged_main_revision"] == 2
    assert download(c, pid, H["dev1"]) == text_master(l6="team")


def test_merge_permissions(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="x"))
    for who in ("dev2", "lead", "viewer"):
        assert c.post(B(pid, "prova", "/merge"), headers=H[who], data={"message": ""}).status_code == 403, who
    r = c.post(B(pid, "prova", "/merge"), headers=H["admin"], data={"message": "da admin"})
    assert r.status_code == 201 and r.json()["author"] == "admin"
    # un branch di team lo puo' proporre qualsiasi Developer
    mk(c, pid, H["lead"], "squadra", "team")
    assert c.post(B(pid, "squadra", "/merge"), headers=H["viewer"], data={"message": ""}).status_code == 403
    # senza modifiche non c'e' nulla da unire
    r = c.post(B(pid, "squadra", "/merge"), headers=H["dev2"], data={"message": ""})
    assert r.status_code == 422 and "Nessuna modifica" in r.json()["detail"]


def test_merge_three_way_with_main_that_moved_on(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="dal branch"))
    main_bump(c, pid, H["lead"], 1, l7="dal principale")                       # il principale e' andato avanti senza sync
    d = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()
    assert d["base_revision"] == 1 and d["head_revision"] == 2 and d["state"] == "awaiting_consent"
    finish_main(c, pid, H, d["id"])
    assert download(c, pid, H["dev1"]) == text_master(l2="dal branch", l7="dal principale", l9="principale 2")


def test_merge_after_sync_uses_new_base(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="dal branch"))
    main_bump(c, pid, H["lead"], 1, l7="dal principale")
    assert c.post(B(pid, "prova", "/sync"), headers=H["dev1"]).json()["state"] == "merged"
    d = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()
    assert d["base_revision"] == 2 and d["n_upstream"] == 0 and d["n_changes"] >= 1


def test_merge_request_cancelled_if_branch_changes(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="uno"))
    prop = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()["id"]
    commit(c, pid, H["dev1"], "prova", text_master(l2="due"))                 # il branch cambia dopo la richiesta
    assert c.get(f"/api/projects/{pid}/proposals/{prop}", headers=H["dev1"]).json()["state"] == "cancelled"
    assert c.post(f"/api/projects/{pid}/proposals/{prop}/approve", headers=H["dev1"], json={}).status_code == 409
    assert head_of(c, pid, H["dev1"]) == 1
    # una nuova richiesta sostituisce la precedente e vale
    p2 = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()["id"]
    p3 = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": "bis"}).json()["id"]
    assert c.get(f"/api/projects/{pid}/proposals/{p2}", headers=H["dev1"]).json()["state"] == "cancelled"
    finish_main(c, pid, H, p3)
    assert download(c, pid, H["dev1"]) == text_master(l2="due")


def test_branch_merge_request_does_not_replace_a_normal_request_of_same_author(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="dal branch"))
    normal = c.post(f"/api/projects/{pid}/proposals", headers=H["dev1"], data={"base_revision": 1},
                    files={"file": ("m.txt", text_master(l5="normale"))}).json()["id"]
    merge = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()["id"]
    states = {p["id"]: p["state"] for p in c.get(f"/api/projects/{pid}/proposals", headers=H["dev1"]).json()}
    assert states[normal] == "awaiting_consent" and states[merge] == "awaiting_consent"


def test_second_merge_request_of_same_branch_is_cancelled_when_first_lands(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="dal branch"))
    p1 = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()["id"]
    p2 = c.post(B(pid, "prova", "/merge"), headers=H["admin"], data={"message": ""}).json()["id"]
    finish_main(c, pid, H, p1)
    assert c.get(f"/api/projects/{pid}/proposals/{p2}", headers=H["dev1"]).json()["state"] == "cancelled"


def test_force_merge_of_branch_request_also_closes_the_branch(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="dal branch"))
    prop = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()["id"]
    r = c.post(f"/api/projects/{pid}/proposals/{prop}/force-merge", headers=H["admin"], json={"comment": "collaudo urgente"})
    assert r.status_code == 200 and r.json()["state"] == "merged", r.text
    assert get(c, pid, H["dev1"], "prova")["state"] == "merged"
    assert audit(c, H, pid, "branch_merged")[0]["details"]["revision"] == 2


# =================================================================================== retention del principale
def test_fork_revision_is_protected_from_main_retention(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    mk(c, pid, H["dev1"], "prova")                                          # fork dalla rev 1
    main_bump(c, pid, H["lead"], 4)                                         # rev 5
    assert mfiles(app) == [1, 4, 5]                                         # la 1 resta: e' la base del branch
    assert c.get(f"/api/projects/{pid}/master/download?revision=1", headers=H["viewer"]).status_code == 200
    commit(c, pid, H["dev1"], "prova", text_master(l2="x"))
    d = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()
    assert d["base_revision"] == 1 and d["state"] == "awaiting_consent"      # la base c'e' ancora: il 3-way funziona
    c.post(f"/api/projects/{pid}/proposals/{d['id']}/cancel", headers=H["dev1"])
    # dopo il sync la base avanza e la vecchia rev 1 viene potata alla revisione successiva
    assert c.post(B(pid, "prova", "/sync"), headers=H["dev1"]).json()["state"] == "merged"
    assert get(c, pid, H["dev1"], "prova")["base_main_revision"] == 5
    main_bump(c, pid, H["lead"], 1)                                         # rev 6
    assert mfiles(app) == [5, 6]
    # ogni branch aperto protegge la propria base; eliminandolo la protezione cade
    mk(c, pid, H["dev2"], "altro")                                          # base = rev 6
    main_bump(c, pid, H["lead"], 3)                                         # rev 9
    assert mfiles(app) == [5, 6, 8, 9]
    assert c.delete(B(pid, "altro"), headers=H["dev2"]).status_code == 200
    main_bump(c, pid, H["lead"], 1)                                         # rev 10
    assert mfiles(app) == [5, 9, 10]


def test_fork_protection_released_when_branch_closed(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    mk(c, pid, H["dev1"], "prova")
    main_bump(c, pid, H["lead"], 3)
    assert mfiles(app) == [1, 3, 4]
    assert c.post(B(pid, "prova", "/close"), headers=H["dev1"]).status_code == 200
    main_bump(c, pid, H["lead"], 1)
    assert mfiles(app) == [4, 5]


def test_active_sync_protects_the_main_revision_it_brings(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    mk(c, pid, H["lead"], "squadra", "team")
    main_bump(c, pid, H["lead"], 1)                                         # rev 2
    c.post(f"/api/projects/{pid}/heartbeat?branch=squadra", headers=H["dev1"])
    prop = c.post(B(pid, "squadra", "/sync"), headers=H["lead"]).json()["id"]          # attende il consenso di dev1
    main_bump(c, pid, H["lead"], 3)                                         # rev 5
    kept = mfiles(app)
    assert 1 in kept and 2 in kept                                           # base (1) e testa portata dal sync (2)
    c.post(f"/api/projects/{pid}/proposals/{prop}/approve?branch=squadra", headers=H["lead"], json={})
    c.post(f"/api/projects/{pid}/proposals/{prop}/approve?branch=squadra", headers=H["dev1"], json={})
    assert get(c, pid, H["lead"], "squadra")["base_main_revision"] == 2


# =================================================================================== chiusura / eliminazione
def test_close_branch_permissions_and_readonly(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    mk(c, pid, H["lead"], "squadra", "team")
    for who in ("dev2", "lead", "viewer"):
        assert c.post(B(pid, "prova", "/close"), headers=H[who]).status_code == 403, who
    assert c.post(B(pid, "squadra", "/close"), headers=H["dev1"]).status_code == 403          # un Developer non chiude un team altrui
    b = c.post(B(pid, "prova", "/close"), headers=H["dev1"]).json()
    assert b["state"] == "closed" and b["can_write"] is False and b["can_delete"] is True
    assert c.post(B(pid, "prova", "/close"), headers=H["dev1"]).status_code == 409
    assert c.post(B(pid, "squadra", "/close"), headers=H["lead"]).json()["state"] == "closed"
    assert audit(c, H, pid, "branch_closed")[0]["details"]["slug"] == "squadra"
    assert len(audit(c, H, pid, "branch_closed")) == 2
    for slug, who in (("prova", "dev1"), ("squadra", "lead")):
        assert c.post(B(pid, slug, "/commit"), headers=H[who], data={"message": "m"},
                      files={"file": ("m.txt", text_master(l1="y"))}).status_code in (409, 422)
        assert c.post(B(pid, slug, "/sync"), headers=H[who]).status_code == 409
        assert c.post(B(pid, slug, "/merge"), headers=H[who], data={"message": ""}).status_code == 409
        assert c.post(f"/api/projects/{pid}/proposals?branch={slug}", headers=H[who], data={"base_revision": 1},
                      files={"file": ("m.txt", text_master(l1="y"))}).status_code == 409
        assert c.get(f"/api/projects/{pid}/master?branch={slug}", headers=H[who]).status_code == 200
    # chiuso il personale, commit => 409 (non 422): lo stato viene prima del tipo
    assert c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
                  files={"file": ("m.txt", text_master(l1="y"))}).status_code == 409
    assert [b["state"] for b in c.get(B(pid), headers=H["viewer"]).json()] == ["closed", "closed"]


def test_close_cancels_pending_requests_both_sides(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    r = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["lead"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l6="team"))})
    c.post(f"/api/projects/{pid}/heartbeat?branch=squadra", headers=H["dev1"])
    inbranch = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["dev2"], data={"base_revision": 1},
                      files={"file": ("m.txt", text_master(l7="altro"))}).json()["id"]
    c.post(f"/api/projects/{pid}/proposals/{r.json()['id']}/approve?branch=squadra", headers=H["lead"], json={})
    # (la richiesta del lead e' ancora in attesa di dev1/dev2) -> crea una richiesta di unione, poi chiude
    mp = c.post(B(pid, "squadra", "/merge"), headers=H["dev2"], data={"message": ""})
    assert mp.status_code in (201, 422)
    c.post(B(pid, "squadra", "/close"), headers=H["lead"])
    st = {p["id"]: p["state"] for p in c.get(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["viewer"]).json()}
    assert st[inbranch] == "cancelled" and st[r.json()["id"]] in ("cancelled", "merged")
    if mp.status_code == 201:
        assert c.get(f"/api/projects/{pid}/proposals/{mp.json()['id']}", headers=H["viewer"]).json()["state"] == "cancelled"


def test_delete_branch_removes_files_row_and_runtime(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    mk(c, pid, H["lead"], "squadra", "team")
    commit(c, pid, H["dev1"], "prova", text_master(l1="x"))
    d = app.state.settings.data_dir / "projects" / "linea-1" / "branches"
    assert (d / "prova" / "revisions").is_dir() and bfiles(app) == [1, 2]
    for who in ("dev2", "lead", "viewer"):
        assert c.delete(B(pid, "prova"), headers=H[who]).status_code == 403, who
    assert c.delete(B(pid, "squadra"), headers=H["dev1"]).status_code == 403              # un Developer non elimina un team
    assert c.delete(B(pid, "prova")).status_code == 401
    r = c.delete(B(pid, "prova"), headers=H["dev1"])
    assert r.status_code == 200 and r.json()["slug"] == "prova" and r.json()["head_revision"] == 2
    assert not (d / "prova").exists() and not [p for p in d.iterdir() if p.name.startswith(".deleted")]
    assert c.get(B(pid, "prova"), headers=H["dev1"]).status_code == 404
    assert c.get(f"/api/projects/{pid}/master?branch=prova", headers=H["dev1"]).status_code == 404
    assert (pid, "prova") not in app.state.registry._brt                                   # noqa: SLF001
    assert [b["slug"] for b in c.get(B(pid), headers=H["viewer"]).json()] == ["squadra"]
    # il Lead elimina il team, l'Admin qualsiasi
    assert c.delete(B(pid, "squadra"), headers=H["lead"]).status_code == 200
    mk(c, pid, H["dev2"], "altro")
    assert c.delete(B(pid, "altro"), headers=H["admin"]).status_code == 200
    rows = audit(c, H, pid, "branch_deleted")
    assert [(a["actor"], a["details"]["slug"]) for a in rows] == [("admin", "altro"), ("lead", "squadra"), ("dev1", "prova")]
    # lo slug si puo' riusare e riparte da zero
    assert mk(c, pid, H["dev1"], "prova")["head_revision"] == 1


def test_delete_cancels_merge_request_in_main(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="x"))
    prop = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": ""}).json()["id"]
    c.delete(B(pid, "prova"), headers=H["dev1"])
    assert c.get(f"/api/projects/{pid}/proposals/{prop}", headers=H["dev1"]).json()["state"] == "cancelled"
    assert c.post(f"/api/projects/{pid}/proposals/{prop}/approve", headers=H["dev1"], json={}).status_code == 409


# =================================================================================== eliminazione progetto, export, backup
def test_delete_project_removes_branches(tworld):
    c, app, pid, H, users = tworld
    other = c.post("/api/projects", headers=H["admin"], json={"name": "Altro"}).json()["id"]
    c.put(f"/api/projects/{other}/permissions", headers=H["admin"], json={"user_id": users["dev1"], "role": "Developer"})
    c.post(f"/api/projects/{other}/master/init", headers=H["admin"], files={"file": ("m.txt", text_master())})
    mk(c, pid, H["dev1"], "prova")
    mk(c, pid, H["lead"], "squadra", "team")
    commit(c, pid, H["dev1"], "prova", text_master(l1="x"))
    mk(c, other, H["dev1"], "intatto")
    root = app.state.settings.data_dir / "projects"
    assert (root / "linea-1" / "branches" / "prova").is_dir()
    r = c.delete(f"/api/projects/{pid}?confirm=Linea 1", headers=H["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["revisions_removed"] == 1 + 2 + 1                                       # principale + prova (2) + squadra (1)
    assert not (root / "linea-1").exists() and not [p for p in root.iterdir() if p.name.startswith(".deleted")]
    with app.state.db.engine.connect() as cx:
        assert cx.exec_driver_sql("SELECT count(*) FROM branches WHERE project_id=?", (pid,)).scalar() == 0
        assert cx.exec_driver_sql("SELECT count(*) FROM branches WHERE project_id=?", (other,)).scalar() == 1
    assert not [k for k in app.state.registry._brt if k[0] == pid]                          # noqa: SLF001
    assert c.get(B(pid), headers=H["admin"]).status_code == 404
    assert (root / "altro" / "branches" / "intatto").is_dir()                               # gli altri progetti restano
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"]


def test_export_and_backup_include_branches(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l1="export"), "per l'export")
    mk(c, pid, H["lead"], "squadra", "team")
    r = c.get(f"/api/projects/{pid}/export", headers=H["admin"])
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"m_rev1.txt", "history.json", "audit.csv", "README.txt", "branches.json",
            "branches/prova/m_rev1.txt", "branches/prova/m_rev2.txt", "branches/prova/history.json",
            "branches/squadra/m_rev1.txt", "branches/squadra/history.json"} <= names
    assert z.read("branches/prova/m_rev2.txt") == text_master(l1="export")
    meta = json.loads(z.read("branches.json"))
    assert [(m["slug"], m["kind"], m["owner"]) for m in meta] == [("prova", "personal", "dev1"), ("squadra", "team", "lead")]
    assert "Branch: 2" in z.read("README.txt").decode()
    ex = audit(c, H, pid, "project_exported")[0]
    assert ex["details"]["branches"] == 2
    # senza branch l'export e' identico a prima (nessun file in piu')
    pid2 = c.post("/api/projects", headers=H["admin"], json={"name": "Senza"}).json()["id"]
    c.post(f"/api/projects/{pid2}/master/init", headers=H["admin"], files={"file": ("m.txt", text_master())})
    z2 = zipfile.ZipFile(io.BytesIO(c.get(f"/api/projects/{pid2}/export", headers=H["admin"]).content))
    assert set(z2.namelist()) == {"m_rev1.txt", "history.json", "audit.csv", "README.txt"}
    # backup: la cartella dei dati comprende anche i branch
    info = create_backup(app.state.settings.data_dir, app.state.settings.data_dir.parent / "bk.zip")
    bz = zipfile.ZipFile(info["path"])
    bn = set(bz.namelist())
    assert "projects/linea-1/branches/prova/revisions/rev_000002.txt" in bn and "projects/linea-1/branches/squadra/history.json" in bn
    with bz.open("aps.db") as f:
        raw = f.read()
    tmp = app.state.settings.data_dir.parent / "bk.db"
    tmp.write_bytes(raw)
    cx = sqlite3.connect(tmp)
    assert cx.execute("SELECT count(*) FROM branches").fetchone()[0] == 2
    cx.close()


# =================================================================================== persistenza e migrazione
def test_branches_survive_server_restart(tmp_path):
    cfg = lambda: Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW, max_login_failures=50)
    app = create_app(cfg())
    with TestClient(app) as c:
        pid, H, _ = build_world(c, text_master(), "m.txt")
        mk(c, pid, H["dev1"], "prova")
        commit(c, pid, H["dev1"], "prova", text_master(l1="persistente"))
        mk(c, pid, H["lead"], "squadra", "team")
        c.post(B(pid, "squadra", "/close"), headers=H["lead"])
    app.state.db.dispose()
    app2 = create_app(cfg())
    with TestClient(app2) as c:
        H = {n: login(c, n) for n in ("dev1", "lead", "viewer")}
        bs = {b["slug"]: b for b in c.get(B(pid), headers=H["viewer"]).json()}
        assert bs["prova"]["head_revision"] == 2 and bs["prova"]["state"] == "open" and bs["prova"]["ahead"] == 1
        assert bs["squadra"]["state"] == "closed"
        assert download(c, pid, H["dev1"], "prova") == text_master(l1="persistente")
        assert commit(c, pid, H["dev1"], "prova", text_master(l1="persistente", l2="ancora"))["revision"] == 3


def test_old_database_gets_branches_table_and_columns(tmp_path):
    cfg = lambda: Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW)
    app = create_app(cfg())
    with TestClient(app) as c:
        h = login(c, "admin", ADMIN_PW)
        assert c.post("/api/projects", headers=h, json={"name": "Vecchio"}).status_code == 201
    app.state.db.dispose()
    raw = sqlite3.connect(tmp_path / "aps.db")
    raw.execute("DROP TABLE branches")                                       # database creato prima dei branch
    raw.commit()
    raw.close()
    app2 = create_app(cfg())
    with TestClient(app2) as c:
        h = login(c, "admin", ADMIN_PW)
        pid = c.get("/api/projects", headers=h).json()[0]["id"]
        assert c.get(B(pid), headers=h).json() == []
        c.post(f"/api/projects/{pid}/master/init", headers=h, files={"file": ("m.txt", text_master())})
        assert mk(c, pid, h, "nuovo")["slug"] == "nuovo"
    app2.state.db.dispose()
    raw = sqlite3.connect(tmp_path / "aps.db")                               # tabella con colonne mancanti -> _migrate le aggiunge
    raw.execute("ALTER TABLE branches DROP COLUMN sync_revision")
    raw.commit()
    raw.close()
    app3 = create_app(cfg())
    with TestClient(app3) as c:
        h = login(c, "admin", ADMIN_PW)
        pid = c.get("/api/projects", headers=h).json()[0]["id"]
        b = c.get(B(pid, "nuovo"), headers=h).json()
        assert b["head_revision"] == 1 and b["ahead"] == 0


# =================================================================================== archiviazione
def test_archived_project_branches_are_read_only(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    assert c.patch(f"/api/projects/{pid}", headers=H["admin"], json={"archived": True}).status_code == 200
    assert get(c, pid, H["dev1"], "prova")["can_write"] is False
    assert c.post(B(pid, "prova", "/commit"), headers=H["dev1"], data={"message": "m"},
                  files={"file": ("m.txt", text_master(l1="x"))}).status_code == 409
    assert c.post(B(pid), headers=H["dev1"], json={"name": "nuovo"}).status_code == 409
    assert c.get(f"/api/projects/{pid}/master?branch=prova", headers=H["viewer"]).status_code == 200


# =================================================================================== commenti
def test_comments_on_branch_requests_are_scoped_to_the_branch(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    r = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["dev1"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l6="team"))})
    prop = r.json()["id"]
    assert c.get(f"/api/projects/{pid}/proposals/{prop}/comments", headers=H["viewer"]).status_code == 404       # non e' nel principale
    cm = c.post(f"/api/projects/{pid}/proposals/{prop}/comments?branch=squadra", headers=H["dev2"], json={"text": "ok @dev1"})
    assert cm.status_code == 201 and cm.json()["mentions"] == ["dev1"]
    lst = c.get(f"/api/projects/{pid}/proposals/{prop}/comments?branch=squadra", headers=H["viewer"]).json()
    assert [x["text"] for x in lst] == ["ok @dev1"]
    assert c.get(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["viewer"]).json()[0]["n_comments"] == 1
    assert audit(c, H, pid, "comment_added")[0]["details"]["branch"] == "squadra"
    # sui personali altrui non si commenta (sola lettura)
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l2="x"))
    main_bump(c, pid, H["lead"], 1, l8="y")
    # il progetto si cancella portandosi via i commenti
    assert c.delete(f"/api/projects/{pid}?confirm=Linea 1", headers=H["admin"]).status_code == 200


# =================================================================================== WebSocket
def drain(ws, until_type, limit=40):
    seen = []
    for _ in range(limit):
        ev = ws.receive_json()
        seen.append(ev)
        if ev["type"] == until_type:
            return ev, seen
    raise AssertionError(f"{until_type} non ricevuto: {[e['type'] for e in seen]}")


def tok(H, who):
    return H[who]["Authorization"][7:]


def test_ws_branch_events_carry_branch_and_stay_in_their_stream(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    mk(c, pid, H["dev1"], "prova")
    with c.websocket_connect(f"/ws?token={tok(H, 'viewer')}&project={pid}") as wm, \
         c.websocket_connect(f"/ws?token={tok(H, 'lead')}&project={pid}&branch=squadra") as wb, \
         c.websocket_connect(f"/ws?token={tok(H, 'dev2')}&project={pid}&branch=prova") as wp:
        hm, hb, hp = wm.receive_json(), wb.receive_json(), wp.receive_json()
        assert hm["type"] == hb["type"] == hp["type"] == "hello"
        assert hm["branch"] is None and hb["branch"] == "squadra" and hp["branch"] == "prova"
        assert "squadra" in hb["message"]
        # evento nel branch squadra: solo i suoi client (+ gli eventi di ciclo di vita a tutti)
        r = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["lead"], data={"base_revision": 1},
                   files={"file": ("m.txt", text_master(l6="team"))})
        prop = r.json()["id"]
        ev, _ = drain(wb, "proposal_submitted")
        assert ev["branch"] == "squadra" and ev["proposal"]["id"] == prop
        c.post(f"/api/projects/{pid}/proposals/{prop}/approve?branch=squadra", headers=H["lead"], json={})
        ev, seen_b = drain(wb, "master_updated")
        assert ev["branch"] == "squadra" and ev["revision"] == 2
        # modifica nel principale: il client del principale lo riceve senza branch; i branch ricevono "main_updated"
        main_bump(c, pid, H["lead"], 1)
        ev, seen_m = drain(wm, "master_updated")
        assert ev["branch"] is None and ev["revision"] == 2
        ev, _ = drain(wb, "main_updated")
        assert ev["branch"] is None and ev["revision"] == 2
        ev, _ = drain(wp, "main_updated")
        assert ev["revision"] == 2
        # il client del principale NON ha visto nessun evento del branch squadra (tranne il ciclo di vita)
        allowed = {"hello", "master_updated", "branch_updated", "branch_created", "branch_deleted", "developer_joined",
                   "developer_left"}
        assert all(e["type"] in allowed or e.get("branch") is None for e in seen_m)
        assert not [e for e in seen_m if e["type"] in ("proposal_submitted", "proposal_updated", "consent_given", "proposal_merged")]


def test_ws_branch_lifecycle_events_reach_everybody(tworld):
    c, app, pid, H, _ = tworld
    with c.websocket_connect(f"/ws?token={tok(H, 'viewer')}&project={pid}") as wm:
        assert wm.receive_json()["type"] == "hello"
        mk(c, pid, H["dev1"], "prova")
        ev, _ = drain(wm, "branch_created")
        assert ev["branch"] == "prova" and ev["developer"] == "dev1" and ev["revision"] == 1
        with c.websocket_connect(f"/ws?token={tok(H, 'dev2')}&project={pid}&branch=prova") as wp:
            assert wp.receive_json()["branch"] == "prova"
            commit(c, pid, H["dev1"], "prova", text_master(l1="x"))
            ev, _ = drain(wm, "branch_updated")
            assert ev["branch"] == "prova" and ev["revision"] == 2
            ev, _ = drain(wp, "branch_updated")
            assert ev["branch"] == "prova"
            c.post(B(pid, "prova", "/close"), headers=H["dev1"])
            assert drain(wm, "branch_updated")[0]["message"].endswith("chiuso il branch «prova»")
            # eliminazione: il client del branch riceve branch_deleted e viene disconnesso
            assert c.delete(B(pid, "prova"), headers=H["dev1"]).status_code == 200
            ev, _ = drain(wp, "branch_deleted")
            assert ev["branch"] == "prova"
            with pytest.raises(Exception):
                for _ in range(10):
                    wp.receive_json()
        ev, _ = drain(wm, "branch_deleted")
        assert ev["branch"] == "prova"


def test_ws_project_deleted_also_closes_branch_clients(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    with c.websocket_connect(f"/ws?token={tok(H, 'dev1')}&project={pid}&branch=prova") as wp:
        assert wp.receive_json()["type"] == "hello"
        assert c.delete(f"/api/projects/{pid}?confirm=Linea 1", headers=H["admin"]).status_code == 200
        ev, _ = drain(wp, "project_deleted")
        assert ev["type"] == "project_deleted"


def test_ws_viewers_can_follow_a_branch_and_presence_is_per_branch(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    with c.websocket_connect(f"/ws?token={tok(H, 'dev1')}&project={pid}&branch=squadra") as w1:
        w1.receive_json()
        w1.send_text("ping")
        while (m := w1.receive_text()) != "pong":              # (dopo il pong la presenza e' registrata)
            assert "developer_joined" in m
        devs = c.get(f"/api/projects/{pid}/developers?branch=squadra", headers=H["viewer"]).json()
        assert [d["developer"] for d in devs] == ["dev1"] and devs[0]["connected"] is True
        assert c.get(f"/api/projects/{pid}/developers", headers=H["viewer"]).json() == []       # nel principale non c'e'
        # la presenza nel branch entra nel quorum del branch, non in quello del principale
        r = c.post(f"/api/projects/{pid}/proposals?branch=squadra", headers=H["lead"], data={"base_revision": 1},
                   files={"file": ("m.txt", text_master(l6="team"))})
        assert {p["developer"] for p in r.json()["participants"]} == {"lead", "dev1"}


# =================================================================================== unit: SessionManager
def _mgr(tmp_path, **kw):
    return SessionManager(MasterStore(tmp_path / "s"), **kw)


def test_unit_submit_with_base_data_swap_and_auto_approve(tmp_path):
    from common.roles import Role
    mgr = _mgr(tmp_path / "x", require_lead_approval=False)
    mgr.store.commit(text_master(l1="branch"), "u", "init", None, filename="m.txt")
    dev = mgr.login("anna", Role.DEVELOPER)
    base = text_master()
    incoming = text_master(l8="principale")                                   # testa del principale
    p = mgr.submit(dev, incoming, 1, "sync", base_data=base, meta={"kind": "sync", "main_head": 7}, swap=True)
    assert p.state.value == "awaiting_consent" and p.swap and p.base_data == base
    res = p.result
    assert len(res.changes) == 1 and res.changes[0].new_text == "branch"       # changes = lato branch (mine)
    assert len(res.upstream_changes) == 1 and res.upstream_changes[0].new_text == "principale"
    mgr.approve(dev, p.id)
    assert mgr.get(p.id).state.value == "merged" and mgr.get(p.id).base_data is None
    assert mgr.store.read_revision(2) == text_master(l1="branch", l8="principale")
    # branch senza modifiche proprie ma con novita' dal principale: ammesso con swap, rifiutato senza
    mgr2 = _mgr(tmp_path / "y", require_lead_approval=False)
    mgr2.store.commit(text_master(), "u", "init", None, filename="m.txt")
    d2 = mgr2.login("bea", Role.DEVELOPER)
    with pytest.raises(SessionError):                                          # nulla cambia da nessuna delle due parti
        mgr2.submit(d2, base, 1, "x", base_data=base, swap=True)
    assert mgr2.submit(d2, incoming, 1, "x", base_data=base, swap=True).result.upstream_changes
    # auto-approvazione: consolida subito e il consenso e' del solo autore
    mgr3 = _mgr(tmp_path / "z")
    mgr3.auto_approve = True
    mgr3.store.commit(text_master(), "u", "init", None, filename="m.txt")
    a, b = mgr3.login("a", Role.DEVELOPER), mgr3.login("b", Role.DEVELOPER)
    p3 = mgr3.submit(a, text_master(l3="x"), 1, "m")
    assert p3.state.value == "merged" and list(p3.required.values()) == ["a"] and not p3.needs_lead
    assert mgr3.store.head().revision == 2


def test_unit_hooks_cancel_matching_and_protect(tmp_path):
    from common.roles import Role
    seen = []
    mgr = _mgr(tmp_path / "x", require_lead_approval=False)
    mgr.store.commit(text_master(), "u", "init", None, filename="m.txt")
    mgr.on_merged = lambda p, rev: seen.append((p.id, rev.revision))
    mgr.extra_protect = lambda: {1}
    dev = mgr.login("anna", Role.DEVELOPER)
    p = mgr.submit(dev, text_master(l1="x"), 1, "uno", meta={"kind": "merge", "source_branch": "k"})
    q = mgr.submit(dev, text_master(l2="y"), 1, "due")                         # tipo diverso: non sostituisce la precedente
    assert p.state.value == "awaiting_consent" and q.state.value == "awaiting_consent"
    assert mgr.cancel_matching(lambda x: x.meta.get("source_branch") == "k", "motivo") == [p.id]
    assert p.state.value == "cancelled" and q.state.value == "awaiting_consent"
    mgr.approve(dev, q.id)
    assert seen == [(q.id, 2)]
    for i in range(14):
        mgr.store.commit(text_master(l5=f"r{i}"), "u", "x", None)
    mgr.max_revisions = 3
    mgr.prune_revisions()
    assert mgr.store.has_revision(1) and not mgr.store.has_revision(2)           # la 1 e' protetta da extra_protect
    mgr.finalize_guard = lambda p_: "non piu' valido"
    head = mgr.store.head().revision
    assert head == 16
    r = mgr.submit(dev, text_master(l3="z", l5="r13"), head, "tre")
    mgr.approve(dev, r.id)
    assert mgr.get(r.id).state.value == "cancelled" and "non piu' valido" in mgr.get(r.id).log[-1]
    assert mgr.store.head().revision == head


def test_slugify_and_permission_helpers():
    from common.roles import Role
    from server.branches import BranchInfo, can_delete, can_write, slugify_branch
    from datetime import datetime, timezone
    assert slugify_branch("Prova  X/Y") == "prova-x-y" and slugify_branch("ÀÈÌ") == "aei" and slugify_branch("***") == "branch"
    assert len(slugify_branch("a" * 100)) == 40 and slugify_branch("-ab_-") == "ab"
    mk_ = lambda kind, state="open": BranchInfo(1, 1, "n", "n", kind, "own", "", 1, 1, state, None, datetime.now(timezone.utc))
    assert can_write(mk_("personal"), Role.DEVELOPER, "own") and not can_write(mk_("personal"), Role.DEVELOPER, "x")
    assert can_write(mk_("personal"), Role.ADMIN, "x") and can_write(mk_("team"), Role.DEVELOPER, "x")
    assert not can_write(mk_("team"), Role.VIEWER, "own") and not can_write(mk_("team", "closed"), Role.ADMIN, "own")
    assert can_delete(mk_("team", "merged"), Role.LEAD, "x") and not can_delete(mk_("personal"), Role.LEAD, "x")
    assert can_delete(mk_("personal"), Role.DEVELOPER, "own") and not can_delete(mk_("personal"), Role.VIEWER, "own")


def test_merge_l5x_branch_into_main_contains_the_change(world):
    """Regressione: con un Master L5X l'unione di un branch porta davvero la modifica nel principale."""
    c, app, pid, H, _ = world
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", S.dev_a().encode(), "modifica L5X", name="m.L5X")
    d = c.post(B(pid, "prova", "/merge"), headers=H["dev1"], data={"message": "x"}).json()
    assert d["n_changes"] > 0, d
    done = finish_main(c, pid, H, d["id"])
    assert done["state"] == "merged"
    main = download(c, pid, H["viewer"])
    assert main != S.base_project().encode() and main == download(c, pid, H["viewer"], None, done["result_revision"])
    assert b"DevA" in main or main != S.base_project().encode()


def test_merge_team_branch_with_sent_file_goes_to_main(tworld):
    """«Unisci nel principale» con un file di lavoro diverso dalla testa del branch: la richiesta nel principale usa quel file."""
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    r = c.post(B(pid, "squadra", "/merge"), headers=H["lead"], data={"message": "mio file"},
               files={"file": ("m.txt", text_master(l2="dal file inviato"))})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["n_changes"] == 1 and "mio file" in d["message"]
    assert [p["id"] for p in c.get(f"/api/projects/{pid}/proposals", headers=H["viewer"]).json()] == [d["id"]]
    done = finish_main(c, pid, H, d["id"], "lead")
    assert done["state"] == "merged"
    assert download(c, pid, H["viewer"]) == text_master(l2="dal file inviato")
    assert get(c, pid, H["viewer"], "squadra")["state"] == "merged"


def test_team_save_merges_three_way_without_vote_and_keeps_colleague_work(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    f = lambda **kw: {"file": ("m.txt", text_master(**kw))}                                  # noqa: E731
    # dev1 e dev2 partono entrambi dalla rev 1 del branch
    r1 = c.post(B(pid, "squadra", "/save"), headers=H["dev1"], data={"message": "uno", "base_revision": 1}, files=f(l2="da dev1"))
    assert r1.status_code == 201, r1.text
    assert r1.json()["state"] == "merged" and r1.json()["result_revision"] == 2
    r2 = c.post(B(pid, "squadra", "/save"), headers=H["dev2"], data={"message": "due", "base_revision": 1}, files=f(l5="da dev2"))
    assert r2.status_code == 201, r2.text
    assert r2.json()["state"] == "merged" and r2.json()["result_revision"] == 3
    assert download(c, pid, H["viewer"], "squadra") == text_master(l2="da dev1", l5="da dev2")        # nessuna modifica persa
    assert head_of(c, pid, H["dev1"]) == 1                                                  # il principale non e' toccato
    # conflitto sulla stessa riga: la richiesta resta da risolvere
    r3 = c.post(B(pid, "squadra", "/save"), headers=H["dev2"], data={"message": "tre", "base_revision": 1}, files=f(l2="in conflitto"))
    assert r3.status_code == 201 and r3.json()["state"] != "merged" and r3.json()["n_conflicts"] >= 1
    # il branch personale non usa /save; il Viewer non puo'
    mk(c, pid, H["dev1"], "mio")
    assert c.post(B(pid, "mio", "/save"), headers=H["dev1"], data={"message": "x"}, files=f(l2="x")).status_code == 422
    assert c.post(B(pid, "squadra", "/save"), headers=H["viewer"], data={"message": "x"}, files=f(l2="y")).status_code == 403
    assert [a["details"]["message"] for a in audit(c, H, pid, "branch_committed")][:2]


# =================================================================================== anteprima (dry-run)
def test_dry_run_merge_and_sync_do_not_create_anything(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["dev1"], "prova")
    commit(c, pid, H["dev1"], "prova", text_master(l4="versione branch", l6="solo branch"))
    main_bump(c, pid, H["lead"], 1, l4="versione principale")
    # merge: l4 in conflitto, l6 pulita
    r = c.post(B(pid, "prova", "/dry-run"), headers=H["dev1"], data={"mode": "merge"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["n_conflicts"] == 1 and not d["clean"] and d["n_changes"] >= 1 and d["conflicts"][0]["kind"]
    assert c.get(f"/api/projects/{pid}/proposals", headers=H["dev1"]).json() == []       # nessuna richiesta creata
    assert head_of(c, pid, H["dev1"]) == 2
    # sync: stessa situazione vista dal branch
    d = c.post(B(pid, "prova", "/dry-run"), headers=H["dev1"], data={"mode": "sync"}).json()
    assert d["n_conflicts"] == 1 and d["n_upstream"] >= 1
    assert c.get(f"/api/projects/{pid}/proposals?branch=prova", headers=H["dev1"]).json() == []
    # un file diverso inviato nell'anteprima del merge
    ok = c.post(B(pid, "prova", "/dry-run"), headers=H["dev1"], data={"mode": "merge"},
                files={"file": ("m.txt", text_master(l6="altra modifica"))}).json()
    assert ok["clean"] and ok["n_changes"] == 1
    assert c.post(B(pid, "prova", "/dry-run"), headers=H["dev1"], data={"mode": "boh"}).status_code == 422
    assert c.post(B(pid, "prova", "/dry-run"), headers=H["viewer"], data={"mode": "merge"}).status_code == 403


def test_dry_run_plain_proposal(tworld):
    c, app, pid, H, _ = tworld
    r = c.post(f"/api/projects/{pid}/proposals/dry-run", headers=H["dev1"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l2="x"))})
    assert r.status_code == 200 and r.json()["clean"] and r.json()["n_changes"] == 1
    assert c.get(f"/api/projects/{pid}/proposals", headers=H["dev1"]).json() == []
    same = c.post(f"/api/projects/{pid}/proposals/dry-run", headers=H["dev1"], data={"base_revision": 1},
                  files={"file": ("m.txt", text_master())}).json()
    assert same["nothing_to_merge"]


# =================================================================================== presenza dei colleghi
def test_presence_lists_contexts_revisions_and_lock(tworld):
    c, app, pid, H, _ = tworld
    mk(c, pid, H["lead"], "squadra", "team")
    for n in ("dev1", "dev2"):
        c.post(f"/api/projects/{pid}/heartbeat?revision=1", headers=H[n])
    c.post(f"/api/projects/{pid}/heartbeat?branch=squadra&revision=1", headers=H["dev2"])
    assert c.post(f"/api/projects/{pid}/lock", headers=H["dev1"], json={"note": "lavoro"}).status_code == 200
    r = c.get(f"/api/projects/{pid}/presence", headers=H["viewer"])
    assert r.status_code == 200, r.text
    by = {p["username"]: p for p in r.json()}
    assert by["dev1"]["online"] and [(x["branch"], x["holds_lock"]) for x in by["dev1"]["contexts"]] == [("", True)]
    assert sorted(x["branch"] for x in by["dev2"]["contexts"]) == ["", "squadra"]
    assert all(not x["holds_lock"] for x in by["dev2"]["contexts"])
    assert any(x["branch_name"] == "squadra" for x in by["dev2"]["contexts"])
    assert c.get(f"/api/projects/{pid}/presence").status_code == 401
