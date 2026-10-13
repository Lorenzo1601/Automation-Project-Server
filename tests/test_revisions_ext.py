"""Server: confronto tra revisioni, etichette/blocco delle revisioni, lock esclusivo (presa in carico) e check-in dei binari."""
import io
import zipfile

from fastapi.testclient import TestClient

from server.config import Settings
from server.locks import LockManager
from server.main import create_app
from server.notifier import RecordingSink
from server.revisions_ext import locks_for
from tests import sample_l5x as S
from tests.server_helpers import (ADMIN_PW, build_world, env, login, reimport, text_master, tworld, world)   # noqa: F401
from tests.test_filetypes import l5k

BIN1 = b"\x00\x01\x02ABC\xff" * 20
BIN2 = b"\x00\x01\x02XYZ\xff" * 21
BIN3 = b"\x00\x09\x09QQQ\xff" * 22


def bump(c, pid, h, n):
    cur = c.get(f"/api/projects/{pid}/master", headers=h).json()["revision"]
    for i in range(n):
        r = reimport(c, pid, h, text_master(l9=f"versione {cur + i + 1}"))
        assert r.status_code == 200, r.text
    return cur + n


def actions(c, h, pid, name):
    rows = c.get(f"/api/audit?project_id={pid}&action={name}&limit=500", headers=h["admin"]).json()
    return rows


def rt_of(app, pid):
    return app.state.registry.runtime(pid)


def hist(c, h, pid):
    rows = c.get(f"/api/projects/{pid}/master/history", headers=h).json()
    return {r["revision"]: r for r in sorted(rows, key=lambda r: r["revision"])}


def bin_world(c, **kw):
    return build_world(c, BIN1, "dati.bin", **kw)


def checkin(c, pid, h, data, msg="nuova versione del file", name="dati.bin"):
    return c.post(f"/api/projects/{pid}/master/checkin", headers=h, data={"message": msg}, files={"file": (name, data)})


# ====================================================================================== confronto
def test_compare_l5x_text_and_l5k(world):
    c, app, pid, H, _ = world
    assert reimport(c, pid, H["lead"], S.dev_a().encode(), name="m.L5X").status_code == 200
    r = c.get(f"/api/projects/{pid}/master/compare?from=1&to=2", headers=H["viewer"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["from_info"]["revision"] == 1 and d["to_info"]["revision"] == 2 and d["kind"] == "l5x"
    assert d["changes"] and all(x["origin"] == "proposal" for x in d["changes"])
    assert d["n_added"] + d["n_modified"] + d["n_deleted"] == len(d["changes"]) and d["n_added"] >= 1
    assert any("Rung" in x["path"] or "Routine" in x["path"] for x in d["changes"])
    # al contrario: le aggiunte diventano eliminazioni
    inv = c.get(f"/api/projects/{pid}/master/compare?from=2&to=1", headers=H["viewer"]).json()
    assert inv["n_deleted"] >= 1 and len(inv["changes"]) == len(d["changes"])
    # stesse revisioni / inesistente / parametri mancanti
    assert c.get(f"/api/projects/{pid}/master/compare?from=2&to=2", headers=H["viewer"]).status_code == 422
    assert c.get(f"/api/projects/{pid}/master/compare?from=1&to=9", headers=H["viewer"]).status_code == 404
    assert c.get(f"/api/projects/{pid}/master/compare?from=1", headers=H["viewer"]).status_code == 422


def test_compare_text_and_l5k_formats(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt")
    bump(c, pid, H["lead"], 1)
    d = c.get(f"/api/projects/{pid}/master/compare?from=1&to=2", headers=H["dev1"]).json()
    assert d["kind"] == "text" and d["n_modified"] == 1 and d["changes"][0]["new_text"] == "versione 2"
    assert d["changes"][0]["base_text"] == "riga 9" and d["warnings"]
    # L5K su un progetto a parte
    ha = H["admin"]
    r = c.post("/api/projects", headers=ha, json={"name": "Linea K"})
    kid = r.json()["id"]
    assert c.post(f"/api/projects/{kid}/master/init", headers=ha, files={"file": ("L.L5K", l5k().encode())}).status_code == 200
    assert reimport(c, kid, ha, l5k(r01a="XIC(Z)OTE(B);").encode(), name="L.L5K").status_code == 200
    d = c.get(f"/api/projects/{kid}/master/compare?from=1&to=2", headers=ha).json()
    assert d["kind"] == "l5k" and d["n_modified"] == 1 and "Program[Main]/Routine[R01]" in d["changes"][0]["path"]


def test_compare_binary_422_and_purged_410(env):
    c, app = env
    pid, H, _ = bin_world(c, max_revisions=2)
    for data in (BIN2, BIN3):
        assert reimport(c, pid, H["lead"], data, name="dati.bin").status_code == 200
    r = c.get(f"/api/projects/{pid}/master/compare?from=2&to=3", headers=H["viewer"])
    assert r.status_code == 422 and "non confrontabile" in r.json()["detail"]
    # rev 1 eliminata dal limite (2 conservate)
    assert hist(c, H["viewer"], pid)[1]["purged"]
    r = c.get(f"/api/projects/{pid}/master/compare?from=1&to=3", headers=H["viewer"])
    assert r.status_code == 410 and "non e' piu' conservata" in r.json()["detail"]


def test_compare_text_purged_410(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    bump(c, pid, H["lead"], 2)
    assert c.get(f"/api/projects/{pid}/master/compare?from=1&to=3", headers=H["viewer"]).status_code == 410
    assert c.get(f"/api/projects/{pid}/master/compare?from=3&to=1", headers=H["viewer"]).status_code == 410
    assert c.get(f"/api/projects/{pid}/master/compare?from=2&to=3", headers=H["viewer"]).status_code == 200


# ====================================================================================== etichette e blocco
def patch(c, h, pid, n, **body):
    return c.patch(f"/api/projects/{pid}/master/revisions/{n}", headers=h, json=body)


def test_label_roles_and_audit(tworld):
    c, app, pid, H, _ = tworld
    bump(c, pid, H["lead"], 1)
    r = patch(c, H["lead"], pid, 1, label="  FAT   cliente ")
    assert r.status_code == 200 and r.json()["label"] == "FAT cliente" and r.json()["labeled_by"] == "lead"
    for who in ("dev1", "viewer"):
        assert patch(c, H[who], pid, 1, label="x").status_code == 403
        assert patch(c, H[who], pid, 1, locked=True).status_code == 403
    assert patch(c, H["admin"], pid, 2, label="SAT").status_code == 200
    h = hist(c, H["viewer"], pid)
    assert h[1]["label"] == "FAT cliente" and h[2]["label"] == "SAT" and not h[1]["locked"]
    assert c.get(f"/api/projects/{pid}/master", headers=H["viewer"]).json()["label"] == "SAT"      # testa
    assert patch(c, H["lead"], pid, 1, label="x" * 81).status_code == 422
    assert patch(c, H["lead"], pid, 1).status_code == 422
    assert patch(c, H["lead"], pid, 9, label="x").status_code == 404
    # nessun cambiamento: nessun nuovo audit
    n = len(actions(c, H, pid, "revision_labeled"))
    assert patch(c, H["lead"], pid, 1, label="FAT cliente").status_code == 200
    assert len(actions(c, H, pid, "revision_labeled")) == n == 2
    # togliere l'etichetta
    assert patch(c, H["lead"], pid, 1, label="").json()["label"] == ""
    last = actions(c, H, pid, "revision_labeled")[0]
    assert last["details"]["label"] == "" and last["details"]["previous"] == "FAT cliente"


def test_lock_unlock_roles_and_audit(tworld):
    c, app, pid, H, _ = tworld
    bump(c, pid, H["lead"], 1)
    r = patch(c, H["lead"], pid, 1, locked=True, label="Consegna cliente")
    assert r.status_code == 200 and r.json()["locked"] and r.json()["locked_by"] == "lead" and r.json()["label"] == "Consegna cliente"
    assert patch(c, H["lead"], pid, 1, locked=False).status_code == 403            # lo sblocca solo l'Admin
    assert hist(c, H["viewer"], pid)[1]["locked"]
    assert patch(c, H["lead"], pid, 1, label="Collaudo").status_code == 200       # l'etichetta si puo' cambiare
    r = patch(c, H["admin"], pid, 1, locked=False)
    assert r.status_code == 200 and not r.json()["locked"] and r.json()["locked_by"] == ""
    assert len(actions(c, H, pid, "revision_locked")) == 1 and len(actions(c, H, pid, "revision_unlocked")) == 1
    assert patch(c, H["lead"], pid, 1, locked=False).status_code == 200            # gia' sbloccata: nessun problema
    assert len(actions(c, H, pid, "revision_unlocked")) == 1


def test_locked_revision_survives_prune_and_limit_reduction(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=3)
    assert patch(c, H["lead"], pid, 1, locked=True).status_code == 200
    bump(c, pid, H["lead"], 5)                                                        # rev 1..6
    h = hist(c, H["viewer"], pid)
    assert [n for n, x in h.items() if x["purged"]] == [2, 3]                         # 3 conservate (4,5,6) + 1 bloccata
    assert not h[1]["purged"] and c.get(f"/api/projects/{pid}/master/download?revision=1", headers=H["viewer"]).status_code == 200
    # ridurre il limite non pota la bloccata
    assert c.patch(f"/api/projects/{pid}", headers=H["admin"], json={"max_revisions": 2}).status_code == 200
    h = hist(c, H["viewer"], pid)
    assert [n for n, x in h.items() if not x["purged"]] == [1, 5, 6]
    # sbloccata: alla prossima potatura sparisce
    assert patch(c, H["admin"], pid, 1, locked=False).status_code == 200
    bump(c, pid, H["lead"], 1)
    assert [n for n, x in hist(c, H["viewer"], pid).items() if not x["purged"]] == [6, 7]
    # una eliminata non si etichetta ne' si blocca
    for body in ({"label": "x"}, {"locked": True}):
        r = patch(c, H["lead"], pid, 2, **body)
        assert r.status_code == 409 and "non e' piu' conservata" in r.json()["detail"]


def test_locked_head_does_not_count_and_export_keeps_names(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt", max_revisions=2)
    bump(c, pid, H["lead"], 1)                                                        # rev 2 = testa
    assert patch(c, H["lead"], pid, 2, locked=True, label="FAT").status_code == 200
    bump(c, pid, H["lead"], 3)                                                        # rev 3..5
    kept = [n for n, x in hist(c, H["viewer"], pid).items() if not x["purged"]]
    assert kept == [2, 4, 5]                                                          # 2 bloccata + le ultime 2 non bloccate
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/projects/{pid}/export", headers=H["admin"]).content))
    assert sorted(z.namelist()) == ["README.txt", "audit.csv", "history.json", "m_rev2.txt", "m_rev4.txt", "m_rev5.txt"]


def test_history_json_backward_compatible(env, tmp_path):
    import json
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt")
    hp = app.state.settings.data_dir / "projects" / "linea-1" / "history.json"
    rows = json.loads(hp.read_text("utf-8"))
    for row in rows:                                                                  # file scritto da una versione precedente
        for k in ("label", "locked", "labeled_by", "locked_by"):
            row.pop(k, None)
    hp.write_text(json.dumps(rows), "utf-8")
    from server.storage import MasterStore
    st = MasterStore(hp.parent)
    assert st.head().label == "" and st.head().locked is False and st.locked_count() == 0
    st.set_locked(1, True, "x")
    assert MasterStore(hp.parent).head().locked is True


# ====================================================================================== lock
def lock_url(pid, suffix=""):
    return f"/api/projects/{pid}/lock{suffix}"


def test_lock_acquire_conflict_renew_release(tworld):
    c, app, pid, H, _ = tworld
    sink = RecordingSink()
    rt_of(app, pid).mgr.sink = sink
    free = c.get(lock_url(pid), headers=H["viewer"]).json()
    assert free["locked"] is False and free["ttl_hours"] == 8.0
    assert c.post(lock_url(pid), headers=H["viewer"]).status_code == 403
    r = c.post(lock_url(pid), headers=H["dev1"], json={"note": "modifica HMI"})
    assert r.status_code == 200 and r.json()["holder"] == "dev1" and r.json()["mine"] and r.json()["note"] == "modifica HMI"
    assert 7.9 * 3600 < r.json()["seconds_left"] <= 8 * 3600
    # un altro utente: 409 con chi / da quando / fino a quando
    r = c.post(lock_url(pid), headers=H["dev2"])
    assert r.status_code == 409 and "dev1" in r.json()["detail"] and "fino alle" in r.json()["detail"]
    st = c.get(lock_url(pid), headers=H["viewer"]).json()
    assert st["locked"] and st["holder"] == "dev1" and st["mine"] is False
    # rinnovo: solo l'holder
    assert c.post(lock_url(pid, "/renew"), headers=H["dev2"]).status_code == 403
    assert c.post(lock_url(pid, "/renew"), headers=H["dev1"]).status_code == 200
    # ri-acquisizione dell'holder = rinnovo (nessun nuovo audit)
    assert c.post(lock_url(pid), headers=H["dev1"]).status_code == 200
    assert len(actions(c, H, pid, "lock_acquired")) == 1
    # rilascio: non-holder 403, holder ok
    assert c.request("DELETE", lock_url(pid), headers=H["dev2"]).status_code == 403
    assert c.request("DELETE", lock_url(pid), headers=H["lead"]).status_code == 403
    r = c.request("DELETE", lock_url(pid), headers=H["dev1"])
    assert r.status_code == 200 and r.json()["locked"] is False
    assert len(actions(c, H, pid, "lock_released")) == 1
    assert c.post(lock_url(pid, "/renew"), headers=H["dev1"]).status_code == 409           # niente da rinnovare
    # evento WS lock_changed (con messaggio e developer)
    ev = [e for e, _t in sink.events if e["type"] == "lock_changed"]
    assert [(e["developer"], "preso in carico" in e["message"]) for e in ev] == [("dev1", True), ("dev1", False)]
    # il lead puo' prendere in carico (Developer+) ma il viewer no
    assert c.post(lock_url(pid), headers=H["lead"]).status_code == 200


def test_lock_expires_lazily(tworld):
    c, app, pid, H, _ = tworld
    now = [1_000_000.0]
    lm = locks_for(rt_of(app, pid), app.state.settings)
    lm.clock = lambda: now[0]
    assert c.post(lock_url(pid), headers=H["dev1"]).status_code == 200
    now[0] += 7 * 3600 + 59 * 60
    assert c.get(lock_url(pid), headers=H["viewer"]).json()["locked"] is True
    assert c.post(lock_url(pid, "/renew"), headers=H["dev1"]).status_code == 200          # rinnovo: altre 8 h
    now[0] += 7 * 3600 + 59 * 60
    assert c.get(lock_url(pid), headers=H["viewer"]).json()["locked"] is True
    now[0] += 2 * 60                                                                      # scaduto
    assert c.get(lock_url(pid), headers=H["viewer"]).json()["locked"] is False
    assert c.post(lock_url(pid, "/renew"), headers=H["dev1"]).status_code == 409
    assert c.post(lock_url(pid), headers=H["dev2"]).status_code == 200                    # ora lo prende un altro
    assert c.get(lock_url(pid), headers=H["viewer"]).json()["holder"] == "dev2"
    # durata configurabile da Settings
    app.state.settings.lock_ttl_h = 0.5
    c.request("DELETE", lock_url(pid), headers=H["dev2"])
    r = c.post(lock_url(pid), headers=H["dev1"])
    assert r.json()["ttl_hours"] == 0.5 and 29 * 60 < r.json()["seconds_left"] <= 30 * 60


def test_force_release_admin_only_with_reason(tworld):
    c, app, pid, H, _ = tworld
    assert c.post(lock_url(pid), headers=H["dev1"]).status_code == 200
    assert c.request("DELETE", lock_url(pid), headers=H["lead"], params={"reason": "devo lavorare io"}).status_code == 403
    assert c.request("DELETE", lock_url(pid), headers=H["admin"]).status_code == 422
    assert c.request("DELETE", lock_url(pid), headers=H["admin"], params={"reason": "no"}).status_code == 422
    assert c.get(lock_url(pid), headers=H["viewer"]).json()["holder"] == "dev1"
    r = c.request("DELETE", lock_url(pid), headers=H["admin"], params={"reason": "dev1 in ferie, serve il file"})
    assert r.status_code == 200 and r.json()["locked"] is False
    a = actions(c, H, pid, "lock_forced")
    assert len(a) == 1 and a[0]["actor"] == "admin" and a[0]["details"]["holder"] == "dev1" \
        and "ferie" in a[0]["details"]["reason"]
    # rilasciare un lock gia' libero non e' un errore e non scrive audit
    assert c.request("DELETE", lock_url(pid), headers=H["admin"]).status_code == 200
    assert len(actions(c, H, pid, "lock_forced")) == 1


def test_lock_survives_restart(env):
    c, app = env
    pid, H, _ = build_world(c, text_master(), "m.txt")
    assert c.post(lock_url(pid), headers=H["dev1"], json={"note": "persistente"}).status_code == 200
    cfg = Settings(data_dir=app.state.settings.data_dir, bcrypt_rounds=4, admin_password=ADMIN_PW, max_login_failures=50)
    app2 = create_app(cfg)
    with TestClient(app2) as c2:
        st = c2.get(lock_url(pid), headers=H["viewer"])
        assert st.status_code == 200, st.text
        assert st.json()["locked"] and st.json()["holder"] == "dev1" and st.json()["note"] == "persistente"
        assert c2.post(lock_url(pid), headers=H["dev2"]).status_code == 409
    # il LockManager e' riletto dal file anche senza server
    lm = LockManager(cfg.data_dir / "projects" / "linea-1" / "lock.json")
    assert lm.current().holder == "dev1"


def test_lock_on_uninitialized_master_and_project_scope(env):
    c, app = env
    ha = login(c, "admin", ADMIN_PW)
    pid = c.post("/api/projects", headers=ha, json={"name": "Vuoto"}).json()["id"]
    assert c.get(lock_url(pid), headers=ha).json()["locked"] is False
    assert c.post(lock_url(pid), headers=ha).status_code == 409


# ====================================================================================== check-in dei binari
def test_checkin_requires_active_lock_of_holder(env):
    c, app = env
    pid, H, _ = bin_world(c)
    sink = RecordingSink()
    rt_of(app, pid).mgr.sink = sink
    assert checkin(c, pid, H["viewer"], BIN2).status_code == 403
    r = checkin(c, pid, H["dev1"], BIN2)
    assert r.status_code == 409 and "Prendere in carico" in r.json()["detail"]
    assert c.post(lock_url(pid), headers=H["dev1"]).status_code == 200
    r = checkin(c, pid, H["dev2"], BIN2)
    assert r.status_code == 403 and "dev1" in r.json()["detail"]
    assert checkin(c, pid, H["admin"], BIN2).status_code == 403                    # nemmeno l'Admin, se non e' l'holder
    assert checkin(c, pid, H["dev1"], BIN2, msg="no").status_code == 422
    assert checkin(c, pid, H["dev1"], b"").status_code == 422
    assert checkin(c, pid, H["dev1"], BIN2, name="dati.exe").status_code == 422     # estensione diversa dal Master
    assert checkin(c, pid, H["dev1"], BIN1).status_code == 422                      # identico alla testa
    assert c.get(f"/api/projects/{pid}/master", headers=H["viewer"]).json()["revision"] == 1
    r = checkin(c, pid, H["dev1"], BIN2, msg="firmware 1.2 dal cliente")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["revision"] == 2 and m["updated_by"] == "dev1" and m["kind"] == "binary" and m["ext"] == ".bin"
    assert "firmware 1.2" in m["message"]
    d = c.get(f"/api/projects/{pid}/master/download?revision=2", headers=H["viewer"])
    assert d.content == BIN2 and "dati_rev2.bin" in d.headers["content-disposition"]
    # il lock e' stato rilasciato e ci sono audit ed eventi
    assert c.get(lock_url(pid), headers=H["viewer"]).json()["locked"] is False
    assert len(actions(c, H, pid, "master_checkin")) == 1 and actions(c, H, pid, "master_checkin")[0]["target"] == "rev2"
    assert [a["details"].get("via") for a in actions(c, H, pid, "lock_released")] == ["checkin"]
    types = [e["type"] for e, _t in sink.events]
    assert "master_updated" in types and types[-1] == "lock_changed"
    # secondo check-in senza presa in carico: di nuovo 409
    assert checkin(c, pid, H["dev1"], BIN3).status_code == 409


def test_checkin_triggers_retention_and_keeps_locked(env):
    c, app = env
    pid, H, _ = bin_world(c, max_revisions=2)
    assert patch(c, H["lead"], pid, 1, locked=True).status_code == 200
    for data in (BIN2, BIN3, BIN2 + b"1", BIN3 + b"2"):
        assert c.post(lock_url(pid), headers=H["dev1"]).status_code == 200
        assert checkin(c, pid, H["dev1"], data).status_code == 200
    h = hist(c, H["viewer"], pid)
    assert max(h) == 5 and [n for n, x in h.items() if not x["purged"]] == [1, 4, 5]
    assert any(a["details"]["pruned"] for a in actions(c, H, pid, "revisions_pruned"))


def test_checkin_refused_on_mergeable_kinds(world):
    c, app, pid, H, _ = world                                                      # Master L5X
    assert c.post(lock_url(pid), headers=H["dev1"]).status_code == 200
    r = checkin(c, pid, H["dev1"], S.dev_a().encode(), name="m.L5X")
    assert r.status_code == 422 and "Richiedi Merge" in r.json()["detail"]
    assert c.get(f"/api/projects/{pid}/master", headers=H["viewer"]).json()["revision"] == 1


def test_binary_submit_points_to_checkin(env):
    c, app = env
    pid, H, _ = bin_world(c)
    r = c.post(f"/api/projects/{pid}/proposals", headers=H["dev1"], data={"base_revision": 1, "message": "x"},
               files={"file": ("dati.bin", BIN2)})
    assert r.status_code == 422 and "Prendi in carico" in r.json()["detail"] and "Check-in" in r.json()["detail"]
