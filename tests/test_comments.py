"""Discussione sulle richieste di merge: permessi, thread, ancore, limiti, modifica a tempo, soft delete, conteggio, WS."""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from server.comments import find_mentions
from tests.server_helpers import PW, env, login, new_project, submit, text_master, tworld, world   # noqa: F401


def mk_prop(c, pid, H, who="dev1"):
    r = submit(c, pid, H[who], text_master(l3="mia modifica"), msg="prova")
    assert r.status_code == 201, r.text
    return r.json()["id"]


def url(pid, prop, tail="comments"):
    return f"/api/projects/{pid}/proposals/{prop}/{tail}"


def post(c, pid, prop, h, text, **kw):
    return c.post(url(pid, prop), headers=h, json={"text": text, **kw})


def test_roles_and_basic_thread(tworld):
    c, app, pid, H, users = tworld
    prop = mk_prop(c, pid, H)
    assert c.get(url(pid, prop), headers=H["viewer"]).json() == []
    assert post(c, pid, prop, H["viewer"], "ciao").status_code == 403           # i Viewer sono in sola lettura
    r1 = post(c, pid, prop, H["dev2"], "  Mi sembra ok  ")
    assert r1.status_code == 201
    j1 = r1.json()
    assert j1["text"] == "Mi sembra ok" and j1["author"] == "dev2" and j1["author_role"] == "Developer"
    assert j1["reply_to"] is None and not j1["deleted"] and j1["edited_at"] is None
    r2 = post(c, pid, prop, H["lead"], "Concordo @dev2, ma controlla @nessuno e @viewer.", reply_to=j1["id"])
    assert r2.status_code == 201 and r2.json()["reply_to"] == j1["id"] and r2.json()["author_role"] == "Lead_Engineer"
    assert r2.json()["mentions"] == ["dev2", "viewer"]                        # «nessuno» non esiste
    post(c, pid, prop, H["admin"], "terzo")
    lst = c.get(url(pid, prop), headers=H["viewer"]).json()                   # il Viewer legge
    assert [x["text"][:6] for x in lst] == ["Mi sem", "Concor", "terzo"]
    assert [x["id"] for x in lst] == sorted(x["id"] for x in lst)
    assert lst[1]["reply_to"] == lst[0]["id"]
    s = c.get(f"/api/projects/{pid}/proposals/{prop}", headers=H["viewer"]).json()
    assert s["n_comments"] == 3
    assert [p["n_comments"] for p in c.get(f"/api/projects/{pid}/proposals", headers=H["viewer"]).json()] == [3]


def test_validation_anchor_and_unknown_proposal(tworld):
    c, app, pid, H, users = tworld
    prop, other = mk_prop(c, pid, H), mk_prop(c, pid, H, "dev2")
    assert post(c, pid, prop, H["dev1"], "   \n ").status_code == 422
    assert post(c, pid, prop, H["dev1"], "x" * 2001).status_code == 422
    assert post(c, pid, prop, H["dev1"], "x" * 2000).status_code == 201
    a = post(c, pid, prop, H["dev1"], "su questo punto", anchor="Program[Main]/Routine[R01]/Rung#3")
    assert a.json()["anchor"] == "Program[Main]/Routine[R01]/Rung#3"
    assert post(c, pid, prop, H["dev1"], "senza ancora", anchor="  ").json()["anchor"] is None
    # reply_to deve appartenere alla stessa richiesta
    assert post(c, pid, other, H["dev1"], "risposta", reply_to=a.json()["id"]).status_code == 422
    assert post(c, pid, prop, H["dev1"], "risposta", reply_to=99999).status_code == 422
    # richiesta inesistente
    assert post(c, pid, "nonesiste", H["dev1"], "ciao").status_code == 404
    assert c.get(url(pid, "nonesiste"), headers=H["dev1"]).status_code == 404
    # senza accesso al progetto / senza token
    assert c.get(url(pid, prop)).status_code == 401


def test_edit_window_and_permissions(tworld):
    c, app, pid, H, users = tworld
    prop = mk_prop(c, pid, H)
    cid = post(c, pid, prop, H["dev1"], "originale").json()["id"]
    patch = lambda who, text, i=None: c.patch(f"/api/projects/{pid}/comments/{i or cid}", headers=H[who], json={"text": text})
    assert patch("dev2", "hack").status_code == 403                           # non e' tuo
    assert patch("admin", "hack").status_code == 403                          # nemmeno l'Admin modifica
    assert patch("viewer", "hack").status_code == 403
    assert patch("dev1", "  ").status_code == 422
    r = patch("dev1", "corretto @lead")
    assert r.status_code == 200 and r.json()["text"] == "corretto @lead" and r.json()["edited_at"] is not None
    assert r.json()["mentions"] == ["lead"]
    assert patch("dev1", "x", 9999).status_code == 404
    # oltre 15 minuti: orologio iniettato
    svc = app.state.comments
    real = svc.clock
    svc.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=14, seconds=50)
    assert patch("dev1", "ancora in tempo").status_code == 200
    svc.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=15, seconds=30)
    r = patch("dev1", "troppo tardi")
    assert r.status_code == 403 and "15 minuti" in r.json()["detail"]
    svc.clock = real
    assert c.get(url(pid, prop), headers=H["dev1"]).json()[0]["text"] == "ancora in tempo"


def test_soft_delete_keeps_thread_and_count(tworld):
    c, app, pid, H, users = tworld
    prop = mk_prop(c, pid, H)
    a = post(c, pid, prop, H["dev1"], "radice").json()["id"]
    b = post(c, pid, prop, H["dev2"], "risposta @dev1", reply_to=a).json()["id"]
    cnt = lambda: c.get(f"/api/projects/{pid}/proposals/{prop}", headers=H["viewer"]).json()["n_comments"]
    assert cnt() == 2
    d = lambda who, i: c.delete(f"/api/projects/{pid}/comments/{i}", headers=H[who])
    assert d("dev2", a).status_code == 403                                    # altrui
    assert d("viewer", a).status_code == 403
    r = d("dev1", a)
    assert r.status_code == 200 and r.json()["deleted"] and r.json()["text"] == "[commento eliminato]"
    assert cnt() == 1
    assert d("dev1", a).status_code == 200 and cnt() == 1                     # idempotente, conteggio invariato
    assert d("admin", b).status_code == 200 and cnt() == 0                    # l'Admin elimina commenti altrui
    lst = c.get(url(pid, prop), headers=H["dev1"]).json()
    assert [x["deleted"] for x in lst] == [True, True] and lst[1]["reply_to"] == a       # il thread resta
    assert lst[1]["mentions"] == []
    assert c.patch(f"/api/projects/{pid}/comments/{a}", headers=H["dev1"], json={"text": "x"}).status_code == 409
    assert post(c, pid, prop, H["dev2"], "dopo", reply_to=a).status_code == 201  # si puo' rispondere a un eliminato


def test_comment_in_any_state_and_cross_project_isolation(tworld):
    c, app, pid, H, users = tworld
    prop = mk_prop(c, pid, H)
    assert c.post(f"/api/projects/{pid}/proposals/{prop}/cancel", headers=H["dev1"]).status_code == 200
    assert post(c, pid, prop, H["dev2"], "commento a richiesta annullata").status_code == 201
    pid2 = new_project(c, H["admin"], users, "Linea 2", text_master(), "m.txt")
    cid = c.get(url(pid, prop), headers=H["dev1"]).json()[0]["id"]
    # un commento non e' raggiungibile passando da un altro progetto
    assert c.patch(f"/api/projects/{pid2}/comments/{cid}", headers=H["dev2"], json={"text": "x"}).status_code == 404
    assert c.delete(f"/api/projects/{pid2}/comments/{cid}", headers=H["admin"]).status_code == 404
    assert c.get(url(pid2, prop), headers=H["dev1"]).status_code == 404


def test_audit_actions(tworld):
    c, app, pid, H, users = tworld
    prop = mk_prop(c, pid, H)
    cid = post(c, pid, prop, H["dev1"], "A" * 120).json()["id"]
    c.patch(f"/api/projects/{pid}/comments/{cid}", headers=H["dev1"], json={"text": "B"})
    c.delete(f"/api/projects/{pid}/comments/{cid}", headers=H["admin"])
    rows = {r["action"]: r for r in c.get(f"/api/audit?project_id={pid}&limit=500", headers=H["admin"]).json()
            if r["action"].startswith("comment_")}
    assert set(rows) == {"comment_added", "comment_edited", "comment_deleted"}
    assert rows["comment_added"]["target"] == prop and rows["comment_added"]["actor"] == "dev1"
    assert rows["comment_added"]["details"]["comment_id"] == cid and rows["comment_added"]["details"]["preview"] == "A" * 80
    assert rows["comment_deleted"]["actor"] == "admin" and rows["comment_deleted"]["details"]["comment_author"] == "dev1"
    assert c.get("/api/audit/verify", headers=H["admin"]).json()["ok"] is True


def test_websocket_event_to_other_clients(tworld):
    c, app, pid, H, users = tworld
    prop = mk_prop(c, pid, H)
    tok = lambda u: c.post("/api/auth/login", json={"username": u, "password": PW}).json()["access_token"]

    def until(ws, typ):
        for _ in range(20):
            ev = ws.receive_json()
            if ev["type"] == typ:
                return ev
        raise AssertionError(typ)

    with c.websocket_connect(f"/ws?token={tok('dev2')}&project={pid}") as ws2, \
         c.websocket_connect(f"/ws?token={tok('viewer')}&project={pid}") as wv:
        assert ws2.receive_json()["type"] == "hello" and wv.receive_json()["type"] == "hello"
        cid = post(c, pid, prop, H["dev1"], "ciao @dev2 e @fantasma").json()["id"]
        for w in (ws2, wv):
            ev = until(w, "comment_added")
            assert ev["developer"] == "dev1" and f"#{prop}" in ev["message"] and "dev1 ha commentato" in ev["message"]
            assert ev["mentions"] == ["dev2"] and ev["proposal"]["id"] == prop and ev["proposal"]["n_comments"] == 1
        c.patch(f"/api/projects/{pid}/comments/{cid}", headers=H["dev1"], json={"text": "nuovo"})
        assert until(ws2, "comment_updated")["developer"] == "dev1"
        c.delete(f"/api/projects/{pid}/comments/{cid}", headers=H["dev1"])
        ev = until(ws2, "comment_deleted")
        assert ev["proposal"]["n_comments"] == 0


def test_comments_deleted_with_project_and_not_leaked(tworld):
    c, app, pid, H, users = tworld
    pid2 = new_project(c, H["admin"], users, "Linea 2", text_master(), "m.txt")
    p1, p2 = mk_prop(c, pid, H), None
    r = submit(c, pid2, H["dev1"], text_master(l1="x"), msg="altro")
    p2 = r.json()["id"]
    post(c, pid, p1, H["dev1"], "uno")
    post(c, pid2, p2, H["dev1"], "due")
    assert c.delete(f"/api/projects/{pid}?confirm=Linea 1", headers=H["admin"]).status_code == 200
    with app.state.db.engine.connect() as cx:
        left = cx.execute(sa.text("SELECT project_id, text FROM proposal_comments")).all()
    assert left == [(pid2, "due")]
    assert c.get(url(pid2, p2), headers=H["dev1"]).json()[0]["text"] == "due"


def test_find_mentions():
    assert find_mentions("ciao @Dev1, @dev2. mail a@b.it @@no @lead-1 @dev1") == ["dev1", "dev2", "lead-1"]
    assert find_mentions("") == []
