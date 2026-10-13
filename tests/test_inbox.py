"""Posta in arrivo: notifiche persistenti per richieste di merge, esiti, branch e menzioni (+ email opzionale)."""
from server.config import Settings
from server.inbox import InboxService
from server.mailer import Mailer
from tests import sample_l5x as S
from tests.server_helpers import ADMIN_PW, PW, build_world, env, login, reimport, text_master, tworld, world   # noqa: F401


def inbox(c, h, unread=False):
    r = c.get("/api/notifications", headers=h, params={"unread_only": unread})
    assert r.status_code == 200, r.text
    return r.json()


def count(c, h):
    return c.get("/api/notifications/count", headers=h).json()["unread"]


def test_merge_request_lifecycle_notifications(tworld):
    c, app, pid, H, _ = tworld
    sent = []
    app.state.inbox.mailer = Mailer(app.state.settings, send_fn=sent.append, background=False)
    c.patch("/api/users/" + str(c.get("/api/users", headers=H["admin"]).json()[0]["id"]), headers=H["admin"], json={})
    for n in ("dev2", "lead"):
        c.post(f"/api/projects/{pid}/heartbeat", headers=H[n])                  # presenti nel quorum
    r = c.post(f"/api/projects/{pid}/proposals", headers=H["dev1"], data={"base_revision": 1, "message": "prova"},
               files={"file": ("m.txt", text_master(l2="x"))})
    assert r.status_code == 201, r.text
    prop = r.json()["id"]
    # dev2 (quorum) e lead (Consenso Finale) ricevono la richiesta; l'autore no
    assert [n["kind"] for n in inbox(c, H["dev2"])] == ["merge_request"] and inbox(c, H["dev2"])[0]["ref"] == prop
    assert [n["kind"] for n in inbox(c, H["lead"])] == ["merge_request"]
    assert inbox(c, H["dev1"]) == [] and count(c, H["dev2"]) == 1
    assert inbox(c, H["dev2"])[0]["project_name"] == "Linea 1"
    # rifiuto -> l'autore lo trova
    assert c.post(f"/api/projects/{pid}/proposals/{prop}/reject", headers=H["dev2"], json={"comment": "no"}).status_code == 200
    kinds = [n["kind"] for n in inbox(c, H["dev1"])]
    assert kinds == ["merge_rejected"] and count(c, H["dev1"]) == 1
    # segna come lette
    assert c.post("/api/notifications/read", headers=H["dev1"], json={"ids": None}).json()["unread"] == 0
    assert inbox(c, H["dev1"], unread=True) == [] and len(inbox(c, H["dev1"])) == 1
    # nuova richiesta approvata e unita -> notifica a tutti i partecipanti
    r = c.post(f"/api/projects/{pid}/proposals", headers=H["dev1"], data={"base_revision": 1, "message": "ok"},
               files={"file": ("m.txt", text_master(l5="y"))}).json()
    for who in ("dev1", "dev2", "lead"):
        c.post(f"/api/projects/{pid}/proposals/{r['id']}/approve", headers=H[who], json={})
    assert any(n["kind"] == "merge_merged" for n in inbox(c, H["dev1"]))
    # ognuno vede solo la propria posta
    assert all(n["kind"] != "merge_rejected" for n in inbox(c, H["dev2"]))
    assert c.get("/api/notifications").status_code == 401


def test_branch_updated_and_mentions_and_email(tworld):
    c, app, pid, H, users = tworld
    sent = []
    app.state.inbox.mailer = Mailer(app.state.settings, send_fn=sent.append, background=False)
    c.patch(f"/api/users/{users['dev2']}", headers=H["admin"], json={"email": "dev2@example.com"})
    b = c.post(f"/api/projects/{pid}/branches", headers=H["lead"], json={"name": "Squadra", "kind": "team"}).json()
    r = c.post(f"/api/projects/{pid}/branches/{b['slug']}/save", headers=H["lead"], data={"message": "m"},
               files={"file": ("m.txt", text_master(l3="z"))})
    assert r.status_code == 201, r.text
    # salvataggio nel team branch: gli altri Developer del progetto vengono avvisati, chi ha salvato no
    assert any(n["kind"] == "branch_updated" and n["branch"] == b["slug"] for n in inbox(c, H["dev2"]))
    assert all(n["kind"] != "branch_updated" for n in inbox(c, H["lead"]))
    c.post(f"/api/projects/{pid}/heartbeat", headers=H["dev2"])
    r = c.post(f"/api/projects/{pid}/proposals", headers=H["dev1"], data={"base_revision": 1},
               files={"file": ("m.txt", text_master(l1="q"))}).json()
    c.post(f"/api/projects/{pid}/proposals/{r['id']}/comments", headers=H["dev1"], json={"text": "ciao @dev2 guarda"})
    kinds = [n["kind"] for n in inbox(c, H["dev2"])]
    assert "mention" in kinds
    assert any("dev2@example.com" == m["To"] for m in sent), [m["To"] for m in sent]


def test_notifications_deleted_with_user_and_project(tworld):
    c, app, pid, H, users = tworld
    c.post(f"/api/projects/{pid}/heartbeat", headers=H["dev2"])
    c.post(f"/api/projects/{pid}/proposals", headers=H["dev1"], data={"base_revision": 1},
           files={"file": ("m.txt", text_master(l2="x"))})
    assert count(c, H["dev2"]) == 1
    assert c.delete(f"/api/users/{users['dev2']}", headers=H["admin"]).status_code == 204
