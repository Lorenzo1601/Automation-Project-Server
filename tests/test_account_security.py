"""Blocco persistente dell'account dopo troppi tentativi e scadenza opzionale delle password."""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi.testclient import TestClient

from server.config import Settings
from server.db.models import User
from server.main import create_app
from tests.test_api_e2e import ADMIN_PW, PW, login


def _cfg(tmp_path):
    return Settings(data_dir=tmp_path, bcrypt_rounds=4, admin_password=ADMIN_PW, max_login_failures=3, lockout_seconds=60)


def _bad(c, name, n):
    for _ in range(n):
        c.post("/api/auth/login", json={"username": name, "password": "sbagliata123"})


def test_lock_survives_restart_and_admin_can_unlock(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as c:
        ha, _ = login(c, "admin", ADMIN_PW)
        uid = c.post("/api/users", headers=ha, json={"username": "mario", "display_name": "Mario", "password": PW,
                                                     "role": "Developer", "must_change_password": False}).json()["id"]
        _bad(c, "mario", 3)
        assert c.post("/api/auth/login", json={"username": "mario", "password": PW}).status_code == 429
        locked = [u for u in c.get("/api/users", headers=ha).json() if u["username"] == "mario"][0]
        assert locked["locked_until"]
    with TestClient(create_app(_cfg(tmp_path))) as c2:                 # riavvio: il throttle in memoria e' vuoto
        r = c2.post("/api/auth/login", json={"username": "mario", "password": PW})
        assert r.status_code == 429 and "bloccato" in r.json()["detail"].lower()
        ha, _ = login(c2, "admin", ADMIN_PW)
        r = c2.patch(f"/api/users/{uid}", headers=ha, json={"unlock": True})
        assert r.status_code == 200 and r.json()["locked_until"] is None
        assert c2.post("/api/auth/login", json={"username": "mario", "password": PW}).status_code == 200
        acts = [e["action"] for e in c2.get("/api/audit", headers=ha, params={"limit": 200}).json()]
        assert "account_locked" in acts


def test_password_expiry_is_optional_and_forces_change(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as c:
        ha, _ = login(c, "admin", ADMIN_PW)
        c.post("/api/users", headers=ha, json={"username": "luca", "display_name": "Luca", "password": PW,
                                               "role": "Developer", "must_change_password": False})
        old = datetime.now(timezone.utc) - timedelta(days=40)
        with app.state.db.scope() as s:
            s.execute(sa.update(User).where(User.username == "luca").values(password_changed_at=old))
        assert c.get("/api/auth/policy", headers=ha).json()["password_max_age_days"] == 0
        _, j = login(c, "luca")                                        # senza criterio: nessuna scadenza
        assert j["user"]["must_change_password"] is False
        # i Developer non possono cambiare il criterio
        hl, _ = login(c, "luca")
        assert c.put("/api/auth/policy", headers=hl, json={"password_max_age_days": 5}).status_code == 403
        assert c.put("/api/auth/policy", headers=ha, json={"password_max_age_days": 30}).json()["password_max_age_days"] == 30
        users = {u["username"]: u for u in c.get("/api/users", headers=ha).json()}
        assert users["luca"]["password_expires_at"]
        _, j = login(c, "luca")                                        # 40 giorni > 30: cambio obbligatorio
        assert j["user"]["must_change_password"] is True
        hl = {"Authorization": f"Bearer {j['access_token']}"}
        assert c.post("/api/auth/change-password", headers=hl, json={"old_password": PW, "new_password": "NuovaPass12345"}).status_code == 204
        assert login(c, "luca", "NuovaPass12345")[1]["user"]["must_change_password"] is False
