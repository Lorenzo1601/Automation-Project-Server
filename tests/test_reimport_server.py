"""Reimportazione del Master (file base) su un progetto gia' in corso: POST /master/reimport."""
import hashlib

from tests import sample_l5x as S
from tests.test_api_e2e import ADMIN_PW, PW, env, login, online, submit, world  # noqa: F401  (fixture 'env', 'world')
from tests.test_filetypes import l5k

BIG = "Aggiornamento da Studio 5000"


def reimport(c, pid, h, content, name="Linea_v2.L5X", reason=BIG):
    data = content.encode() if isinstance(content, str) else content
    return c.post(f"/api/projects/{pid}/master/reimport", headers=h, data={"reason": reason}, files={"file": (name, data)})


def _l5k_project(c, H, users, name="Impianto L5K"):
    """Secondo progetto con Master L5K 'Linea1.L5K'; lead/dev1/viewer con accesso."""
    pid = c.post("/api/projects", headers=H["admin"], json={"name": name}).json()["id"]
    for n, role in [("lead", "Lead_Engineer"), ("dev1", "Developer"), ("viewer", "Viewer")]:
        assert c.put(f"/api/projects/{pid}/permissions", headers=H["admin"],
                     json={"user_id": users[n], "role": role}).status_code == 200
    r = c.post(f"/api/projects/{pid}/master/init", headers=H["admin"], files={"file": ("Linea1.L5K", l5k().encode())})
    assert r.status_code == 200, r.text
    return pid


# ----------------------------------------------------------------------------------- percorso felice
def test_reimport_l5x_creates_new_revision_and_keeps_history(world):
    c, app, pid, H, users = world
    new = S.dev_a()
    r = reimport(c, pid, H["lead"], new, name="Linea_v2.L5X")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["revision"] == 2 and j["kind"] == "l5x" and j["ext"] == ".L5X" and j["name"] == "Linea_v2"
    assert j["updated_by"] == "lead" and BIG in j["message"]
    assert j["sha256"] == hashlib.sha256(new.encode()).hexdigest()
    hist = c.get(f"/api/projects/{pid}/master/history", headers=H["viewer"]).json()
    assert [h["revision"] for h in hist] == [2, 1]
    assert hist[1]["name"] == "m" and hist[0]["name"] == "Linea_v2"          # storico intatto
    dl = c.get(f"/api/projects/{pid}/master/download", headers=H["viewer"])
    assert dl.content == new.encode()
    assert 'filename="Linea_v2_rev2.L5X"' in dl.headers["content-disposition"]
    old = c.get(f"/api/projects/{pid}/master/download?revision=1", headers=H["viewer"])
    assert old.content == S.base_project().encode()
    assert 'filename="Linea_v2_rev1.L5X"' in old.headers["content-disposition"]
    assert c.get(f"/api/projects/{pid}/master", headers=H["dev1"]).json()["revision"] == 2
    # la copia comoda nella cartella del progetto segue il nuovo nome
    store = app.state.registry.runtime(pid).store
    assert (store.root / "Linea_v2.L5X").read_bytes() == new.encode() and not (store.root / "m.L5X").exists()


def test_reimport_audit_entry_and_ws_event(world):
    c, app, pid, H, users = world
    tok = login(c, "dev1")[1]["access_token"]
    with c.websocket_connect(f"/ws?token={tok}&project={pid}") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert reimport(c, pid, H["lead"], S.dev_b(), name="Nuovo.L5X", reason="Rebase dopo upgrade").status_code == 200
        ev = ws.receive_json()
        while ev["type"] != "master_updated":
            ev = ws.receive_json()
        assert ev["revision"] == 2 and "reimportato da lead" in ev["message"] and "Rebase dopo upgrade" in ev["message"]
    rows = c.get(f"/api/audit?project_id={pid}&action=master_reimported", headers=H["admin"]).json()
    assert len(rows) == 1
    a = rows[0]
    assert a["actor"] == "lead" and a["target"] == "rev2"
    d = a["details"]
    assert d["previous_head"] == 1 and d["kind"] == "l5x" and d["ext"] == ".L5X" and d["name"] == "Nuovo"
    assert d["filename"] == "Nuovo.L5X" and d["reason"] == "Rebase dopo upgrade"
    assert d["sha256"] == hashlib.sha256(S.dev_b().encode()).hexdigest()
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"]


def test_reimport_l5k(world):
    c, app, pid0, H, users = world
    pid = _l5k_project(c, H, users)
    new = l5k(r01a="XIC(A)XIC(Z)OTE(B);")
    r = reimport(c, pid, H["lead"], new, name="Linea1_nuova.l5k")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["revision"] == 2 and j["kind"] == "l5k" and j["ext"] == ".L5K" and j["name"] == "Linea1_nuova"
    assert [h["revision"] for h in c.get(f"/api/projects/{pid}/master/history", headers=H["viewer"]).json()] == [2, 1]
    dl = c.get(f"/api/projects/{pid}/master/download", headers=H["dev1"])
    assert dl.content == new.encode() and 'filename="Linea1_nuova_rev2.L5K"' in dl.headers["content-disposition"]
    assert c.get(f"/api/projects/{pid}/master/download?revision=1", headers=H["dev1"]).content == l5k().encode()
    # anche l'admin puo' reimportare
    assert reimport(c, pid, H["admin"], l5k(r02="XIC(G)OTE(H2);"), name="Linea1_nuova.L5K").json()["revision"] == 3


def test_reimport_text_project_keeps_extension(world):
    c, app, pid0, H, users = world
    pid = c.post("/api/projects", headers=H["admin"], json={"name": "Testo"}).json()["id"]
    assert c.post(f"/api/projects/{pid}/master/init", headers=H["admin"],
                  files={"file": ("main.st", b"PROGRAM Main\nEND_PROGRAM\n")}).status_code == 200
    assert reimport(c, pid, H["admin"], b"PROGRAM Main\nx := 1;\nEND_PROGRAM\n", name="main.xml").status_code == 422
    r = reimport(c, pid, H["admin"], b"PROGRAM Main\nx := 1;\nEND_PROGRAM\n", name="principale.st")
    assert r.status_code == 200 and r.json()["ext"] == ".st" and r.json()["name"] == "principale"
    dl = c.get(f"/api/projects/{pid}/master/download", headers=H["admin"])
    assert 'filename="principale_rev2.st"' in dl.headers["content-disposition"]


# ------------------------------------------------------------------------------------------ ruoli
def test_reimport_roles(world):
    c, app, pid, H, users = world
    for n in ("dev1", "viewer"):
        assert reimport(c, pid, H[n], S.dev_a()).status_code == 403
    assert c.get(f"/api/projects/{pid}/master", headers=H["lead"]).json()["revision"] == 1
    assert reimport(c, pid, H["outsider"], S.dev_a()).status_code == 403            # senza permessi sul progetto
    assert c.post(f"/api/projects/{pid}/master/reimport", data={"reason": BIG},
                  files={"file": ("a.L5X", b"x")}).status_code == 401
    assert reimport(c, pid, H["lead"], S.dev_a()).status_code == 200


def test_reimport_requires_initialized_master(world):
    c, app, pid0, H, users = world
    pid = c.post("/api/projects", headers=H["admin"], json={"name": "Vuoto"}).json()["id"]
    r = reimport(c, pid, H["admin"], S.dev_a())
    assert r.status_code == 409 and "non inizializzato" in r.json()["detail"]


# ------------------------------------------------------------------------------------ validazioni 422
def test_reimport_validation_errors(world):
    c, app, pid, H, users = world
    lead = H["lead"]
    r = reimport(c, pid, lead, S.dev_a(), reason="  ok  ")
    assert r.status_code == 422 and "motivo" in r.json()["detail"]
    assert reimport(c, pid, lead, S.dev_a(), reason="").status_code == 422
    r = reimport(c, pid, lead, l5k())                                                # tipo diverso (L5K su progetto L5X)
    assert r.status_code == 422 and "tipo" in r.json()["detail"]
    assert reimport(c, pid, lead, b"PROGRAM x\nEND_PROGRAM\n", name="a.st").status_code == 422
    r = reimport(c, pid, lead, S.project(name="AltroImpianto"))                      # Controller diverso
    assert r.status_code == 422 and "AltroImpianto" in r.json()["detail"]
    r = reimport(c, pid, lead, S.base_project())                                     # identico al Master corrente
    assert r.status_code == 422 and "identico al Master corrente" in r.json()["detail"]
    r = reimport(c, pid, lead, b"")
    assert r.status_code == 422 and "vuoto" in r.json()["detail"]
    # un L5X "riconosciuto" ma non valido (XML rotto) viene rifiutato
    broken = S.dev_a().replace("</Controller>", "")
    assert reimport(c, pid, lead, broken).status_code == 422
    assert c.get(f"/api/projects/{pid}/master", headers=lead).json()["revision"] == 1      # nessuna revisione creata
    assert c.get(f"/api/audit?project_id={pid}&action=master_reimported", headers=H["admin"]).json() == []


def test_reimport_l5k_other_controller_and_identical(world):
    c, app, pid0, H, users = world
    pid = _l5k_project(c, H, users)
    r = reimport(c, pid, H["lead"], l5k(name="AltraLinea"), name="x.L5K")
    assert r.status_code == 422 and "AltraLinea" in r.json()["detail"]
    r = reimport(c, pid, H["lead"], l5k(), name="Linea1.L5K")
    assert r.status_code == 422 and "identico al Master corrente" in r.json()["detail"]


def test_reimport_too_large(world):
    c, app, pid, H, users = world
    app.state.settings.max_upload_mb = 0
    r = reimport(c, pid, H["lead"], S.dev_a())
    assert r.status_code == 413


def test_init_still_refuses_second_initialization(world):
    c, app, pid, H, users = world
    r = c.post(f"/api/projects/{pid}/master/init", headers=H["lead"], files={"file": ("m.L5X", S.dev_a().encode())})
    assert r.status_code == 409 and "gia' inizializzato" in r.json()["detail"]


# ------------------------------------------------------------------ richieste in corso dopo il reimport
def test_reimport_reanalyses_pending_proposals_and_resets_consents(world):
    c, app, pid, H, users = world
    online(c, pid, H, "dev1", "dev2")
    prop = submit(c, pid, H["dev1"], S.dev_b(False), msg="allarme").json()
    base = f"/api/projects/{pid}/proposals/{prop['id']}"
    d = c.post(base + "/approve", headers=H["dev1"], json={}).json()
    d = c.post(base + "/approve", headers=H["dev2"], json={}).json()
    assert d["state"] == "awaiting_consent" and d["head_revision"] == 1
    assert sorted(p["decision"] for p in d["participants"]) == ["approve", "approve"]
    # il Lead reimporta un base nuovo (rung 0 modificato): la richiesta viene rianalizzata sulla rev 2
    assert reimport(c, pid, H["lead"], S.dev_a(), name="Base_nuovo.L5X").json()["revision"] == 2
    d = c.get(base, headers=H["viewer"]).json()
    assert d["head_revision"] == 2 and d["base_revision"] == 1 and d["n_upstream"] > 0
    assert d["state"] == "awaiting_consent"
    assert all(p["decision"] == "pending" for p in d["participants"])             # consensi azzerati
    # per chiudere il giro servono di nuovo i consensi, poi il merge parte dal NUOVO Master
    for n in ("dev1", "dev2", "lead"):
        d = c.post(base + "/approve", headers=H[n], json={}).json()
    assert d["state"] == "merged" and d["result_revision"] == 3
    merged = c.get(f"/api/projects/{pid}/master/download", headers=H["viewer"]).content.decode()
    assert "XIO(Fault)" in merged and "XIC(Alarm)XIC(Enable)OTE(Horn);" in merged
