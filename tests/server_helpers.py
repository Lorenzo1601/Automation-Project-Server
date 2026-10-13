"""Fixture condivise dai test server di eliminazione/retention/backup (app FastAPI + utenti + un progetto con Master)."""
import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from tests import sample_l5x as S

ADMIN_PW = "AdminPass123"
PW = "Password12345"
ROLES = [("lead", "Lead_Engineer"), ("dev1", "Developer"), ("dev2", "Developer"), ("viewer", "Viewer")]


def text_master(**over: str) -> bytes:
    """Master di testo di 10 righe; ``over`` = {"l3": "nuovo testo"} sostituisce la riga 3."""
    return "\n".join(over.get(f"l{i}", f"riga {i}") for i in range(10)).encode()


@pytest.fixture
def env(tmp_path):
    cfg = Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW, max_login_failures=50)
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c, app


def login(c, user, pw=PW):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def new_project(c, ha, users, name, master=None, filename="m.L5X", **extra):
    """Crea il progetto, assegna lead/dev1/dev2/viewer e carica il Master. Ritorna l'id."""
    r = c.post("/api/projects", headers=ha, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    for n, role in ROLES:
        assert c.put(f"/api/projects/{pid}/permissions", headers=ha,
                     json={"user_id": users[n], "role": role}).status_code == 200
    data = S.base_project().encode() if master is None else master
    r = c.post(f"/api/projects/{pid}/master/init", headers=ha, files={"file": (filename, data)})
    assert r.status_code == 200, r.text
    return pid


def build_world(c, master=None, filename="m.L5X", **extra):
    ha = login(c, "admin", ADMIN_PW)
    users = {}
    for name, role in ROLES:
        r = c.post("/api/users", headers=ha, json={"username": name, "display_name": name.title(), "password": PW,
                                                    "role": role, "must_change_password": False})
        assert r.status_code == 201, r.text
        users[name] = r.json()["id"]
    pid = new_project(c, ha, users, "Linea 1", master, filename, **extra)
    H = {n: login(c, n) for n in users}
    H["admin"] = ha
    return pid, H, users


@pytest.fixture
def world(env):
    """Progetto 'Linea 1' con Master L5X + utenti lead/dev1/dev2/viewer."""
    c, app = env
    pid, H, users = build_world(c)
    return c, app, pid, H, users


@pytest.fixture
def tworld(env):
    """Come ``world`` ma con un Master di testo (.txt): ogni reimport crea una revisione senza giri di consenso."""
    c, app = env
    pid, H, users = build_world(c, text_master(), "m.txt")
    return c, app, pid, H, users


def reimport(c, pid, h, content: bytes, reason="aggiornamento test", name="m.txt"):
    return c.post(f"/api/projects/{pid}/master/reimport", headers=h, data={"reason": reason},
                  files={"file": (name, content)})


def submit(c, pid, h, content: bytes, base=1, msg="", name="m.txt"):
    return c.post(f"/api/projects/{pid}/proposals", headers=h, data={"base_revision": base, "message": msg},
                  files={"file": (name, content)})
