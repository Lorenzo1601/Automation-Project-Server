"""Aggiornamento automatico del client: pubblicazione sul server, controllo versione, download verificato, script di sostituzione."""
import hashlib
import sys
from pathlib import Path

import pytest

from client import updater
from common.version import is_newer, version_tuple
from tests.server_helpers import ADMIN_PW, PW, build_world, env, login, text_master, world   # noqa: F401

EXE = b"MZ" + b"\x00" * 4000 + b"client-v21"


def test_version_compare():
    assert version_tuple("2.10.1") == (2, 10, 1) and version_tuple("v2.0-beta") == (2, 0) and version_tuple("") == (0,)
    assert is_newer("2.1", "2.0") and is_newer("2.0.1", "2.0") and is_newer("2.10", "2.9")
    assert not is_newer("2.0", "2.0") and not is_newer("2.0.0", "2.0") and not is_newer("1.9", "2.0")


def test_publish_check_download_and_permissions(world, tmp_path):
    c, app, pid, H, _ = world
    # nessun client pubblicato
    r = c.get("/api/client/update", headers=H["dev1"], params={"current": "2.0"})
    assert r.status_code == 200 and r.json() == {**r.json(), "available": False, "version": ""}
    assert c.get("/api/client/download", headers=H["dev1"]).status_code == 404
    # solo l'Admin pubblica
    f = {"file": ("AutomationProjectClient.exe", EXE)}
    assert c.post("/api/client/update", headers=H["dev1"], data={"version": "2.1"}, files=f).status_code == 403
    assert c.post("/api/client/update", headers=H["admin"], data={"version": "boh"}, files=f).status_code == 422
    r = c.post("/api/client/update", headers=H["admin"], data={"version": "2.1", "notes": "Novità"}, files=f)
    assert r.status_code == 201, r.text
    sha = hashlib.sha256(EXE).hexdigest()
    assert r.json()["sha256"] == sha and r.json()["size"] == len(EXE)
    # chi ha la 2.0 vede l'aggiornamento, chi ha la 2.1 no
    assert c.get("/api/client/update", headers=H["dev1"], params={"current": "2.0"}).json()["available"] is True
    assert c.get("/api/client/update", headers=H["dev1"], params={"current": "2.1"}).json()["available"] is False
    assert c.get("/api/client/download", headers=H["dev1"]).content == EXE
    assert c.get("/api/client/download").status_code == 401
    # il piu' recente sostituisce il precedente
    c.post("/api/client/update", headers=H["admin"], data={"version": "2.2"}, files={"file": ("AutomationProjectClient.exe", EXE + b"x")})
    assert c.get("/api/client/update", headers=H["dev1"]).json()["version"] == "2.2"
    assert c.delete("/api/client/update", headers=H["dev1"]).status_code == 403
    assert c.delete("/api/client/update", headers=H["admin"]).status_code == 204
    assert c.get("/api/client/update", headers=H["dev1"], params={"current": "1.0"}).json()["available"] is False
    acts = [e["action"] for e in c.get("/api/audit", headers=H["admin"], params={"limit": 50}).json()]
    assert "client_update_published" in acts and "client_update_removed" in acts


def test_updater_download_verifies_hash_and_script(tmp_path, monkeypatch):
    from common.schemas_inbox import UpdateInfo

    class Api:
        def __init__(self, data):
            self.data = data

        def download_client(self, dest):
            Path(dest).write_bytes(self.data)

    info = UpdateInfo(available=True, version="2.1", sha256=hashlib.sha256(EXE).hexdigest(), filename="Client.exe")
    p = updater.download(Api(EXE), info, tmp_path)
    assert p.read_bytes() == EXE
    with pytest.raises(updater.UpdateError):
        updater.download(Api(EXE + b"manomesso"), info, tmp_path)
    assert not (tmp_path / "Client.exe").exists()                        # file non verificato: eliminato
    # da sorgenti non si sostituisce niente
    with pytest.raises(updater.UpdateError):
        updater.apply(p)
    # congelato: lo script attende il PID, sposta il nuovo exe sul vecchio e lo riavvia
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "AutomationProjectClient.exe"))
    launched = []
    bat = updater.apply(p, launcher=lambda *a, **k: launched.append(a[0]))
    txt = bat.read_text("utf-8")
    assert launched and launched[0][:2] == ["cmd", "/c"]
    assert str(p) in txt and "AutomationProjectClient.exe" in txt and "move /Y" in txt and "start" in txt


def test_server_window_publishes_client_update(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from client.theme import apply_dark_theme
    from server.gui.main_window import ServerWindow
    from server.gui.settings import ServerPrefs
    from tests.test_backup_export import _sync
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    prefs = ServerPrefs(QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat))
    prefs.data_base, prefs.host, prefs.port, prefs.autostart = str(tmp_path / "dati"), "127.0.0.1", 8798, False
    w = ServerWindow(prefs, run=_sync, confirm_close=False)
    w._pending_admin = ("admin", ADMIN_PW)
    assert w.start()
    try:
        exe = tmp_path / "Client.exe"
        exe.write_bytes(EXE)
        w.refresh_client_update()
        assert "Nessun client" in w.settings_page.lbl_client_update.text() and not w.settings_page.btn_unpublish_client.isEnabled()
        w.publish_client(str(exe), "2.1", "note")
        assert "versione 2.1" in w.settings_page.lbl_client_update.text() and w.settings_page.btn_unpublish_client.isEnabled()
        assert w.server.client_update().sha256 == hashlib.sha256(EXE).hexdigest()
        w.unpublish_client()
        assert "Nessun client" in w.settings_page.lbl_client_update.text()
    finally:
        w.close()
        w.server and w.server.detach_logging()
