"""Test della GUI del server: controller (cartella dati, console in-process, avvio/arresto) e finestra (offscreen)."""
import os
import socket
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings                                   # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox                # noqa: E402

from client.api_client import ApiError                                 # noqa: E402
from client.theme import apply_dark_theme                              # noqa: E402
from common.schemas import UserCreate, UserUpdate                      # noqa: E402
from server.gui.controller import (DATA_SUBDIR, EmbeddedServer, check_writable, copy_data,   # noqa: E402
                                   has_data, port_in_use, resolve_data_dir)
from server.gui.main_window import ServerWindow                        # noqa: E402
from server.gui.settings import ServerPrefs                            # noqa: E402
from server.gui.setup_dialog import ChangeFolderDialog, SetupDialog    # noqa: E402

ADMIN_PW = "AdminPass123"
PW = "Password12345"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    return app


@pytest.fixture(autouse=True)
def _close_widgets(qapp):
    yield
    from PySide6.QtCore import QEvent
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def sync(fn, ok=None, err=None):
    try:
        res = fn()
    except Exception as exc:                                           # noqa: BLE001
        if err:
            err(exc)
        return
    if ok:
        ok(res)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def srv(tmp_path):
    s = EmbeddedServer(resolve_data_dir(tmp_path), "127.0.0.1", free_port(), admin_password=ADMIN_PW)
    s.open()
    yield s
    s.close()
    s.detach_logging()


# ======================================================================================= cartella dati
def test_resolve_data_dir_creates_subfolder_but_never_nests(tmp_path):
    assert resolve_data_dir(tmp_path) == tmp_path / DATA_SUBDIR
    assert resolve_data_dir(tmp_path / DATA_SUBDIR) == tmp_path / DATA_SUBDIR          # gia' una cartella dati
    (tmp_path / "vecchio").mkdir()
    (tmp_path / "vecchio" / "aps.db").write_bytes(b"x")
    assert resolve_data_dir(tmp_path / "vecchio") == tmp_path / "vecchio"              # contiene gia' un database
    assert has_data(tmp_path / "vecchio") and not has_data(tmp_path)


def test_check_writable_and_copy_data(tmp_path):
    assert check_writable(tmp_path / "a" / "b") is None and (tmp_path / "a" / "b").is_dir()
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    assert check_writable(blocker / "sotto") is not None                               # non si puo' creare sotto un file
    src = tmp_path / "src"
    (src / "projects" / "1").mkdir(parents=True)
    (src / "aps.db").write_bytes(b"db")
    (src / "aps.db-wal").write_bytes(b"wal")
    (src / "projects" / "1" / "rev1.L5X").write_text("rev")
    assert copy_data(src, tmp_path / "dst") == 2
    assert (tmp_path / "dst" / "projects" / "1" / "rev1.L5X").read_text() == "rev"
    assert not (tmp_path / "dst" / "aps.db-wal").exists()
    with pytest.raises(ValueError):
        copy_data(src, src / "dentro")


# ======================================================================================= console in-process
def test_console_creates_users_without_password_and_server_stopped(srv):
    api = srv.console
    assert api.user.username == "admin" and api.role.name == "ADMIN" and not srv.running
    api.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer"))
    assert {u.username for u in api.users()} == {"admin", "mario"}
    acts = [a.action for a in api.audit()]
    assert "console_session" in acts and "user_created" in acts


def test_console_bypasses_forced_password_change_and_survives_admin_reset(srv):
    api = srv.console
    admin = next(u for u in api.users() if u.username == "admin")
    api.update_user(admin.id, UserUpdate(new_password="NuovaPassword99"))        # revoca anche la sessione console
    assert api.users()                                                         # la console ne riapre una da sola
    assert next(u for u in api.users() if u.username == "admin").must_change_password
    from server.auth.models import AuthError
    with pytest.raises(AuthError):                                             # i token console non sono falsificabili
        srv.app.state.auth.principal_from_access("x.y.z")


def test_regular_login_still_requires_password_change(srv):
    api = srv.console
    api.create_user(UserCreate(username="nuovo", display_name="N", password=PW, role="Developer",
                               must_change_password=True))
    srv.start()
    try:
        r = httpx.post(f"http://127.0.0.1:{srv.port}/api/auth/login", json={"username": "nuovo", "password": PW})
        tok = r.json()["access_token"]
        r2 = httpx.get(f"http://127.0.0.1:{srv.port}/api/projects", headers={"Authorization": f"Bearer {tok}"})
        assert r2.status_code == 403                                            # il bypass vale solo per le sessioni console
    finally:
        srv.stop()


def test_start_stop_restart_and_login_over_network(srv):
    srv.console.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer"))
    assert srv.start() and srv.running
    url = f"http://127.0.0.1:{srv.port}"
    assert httpx.get(url + "/api/health").status_code == 200
    r = httpx.post(url + "/api/auth/login", json={"username": "mario", "password": PW})
    assert r.status_code == 200
    assert srv.connections() == 0
    srv.stop()
    assert not srv.running
    with pytest.raises(httpx.HTTPError):
        httpx.get(url + "/api/health", timeout=1)
    srv.console.create_user(UserCreate(username="anna", display_name="Anna", password=PW, role="Viewer"))   # a server fermo
    assert srv.start()                                                          # riavvio sullo stesso database
    assert httpx.post(url + "/api/auth/login", json={"username": "anna", "password": PW}).status_code == 200


def test_start_fails_cleanly_when_port_busy(srv):
    busy = socket.socket()
    busy.bind(("127.0.0.1", srv.port))
    busy.listen(1)
    try:
        assert port_in_use("127.0.0.1", srv.port)
        assert srv.start() is False and "porta" in srv.last_error.lower() and not srv.running
        assert srv.console.users()                                              # la console funziona comunque
    finally:
        busy.close()


# ======================================================================================= dialoghi
def test_setup_dialog_validation(qapp, tmp_path):
    d = SetupDialog(base=str(tmp_path))
    d.pw.setText("corta1")
    d.pw2.setText("corta1")
    d._accept()
    assert "10 caratteri" in d.error.text() and d.result() == 0
    d.pw.setText(PW)
    d.pw2.setText("Diversa12345")
    d._accept()
    assert "coincidono" in d.error.text()
    d.pw2.setText(PW)
    d.port.setValue(9000)
    d._accept()
    v = d.values()
    assert d.result() == 1 and v["base"] == str(tmp_path) and v["admin_password"] == PW and v["port"] == 9000


def test_setup_dialog_reuses_existing_data(qapp, tmp_path):
    (tmp_path / DATA_SUBDIR).mkdir()
    (tmp_path / DATA_SUBDIR / "aps.db").write_bytes(b"x")
    d = SetupDialog(base=str(tmp_path))
    assert d.folder.existing() and d.admin_box.isHidden()
    d._accept()
    assert d.result() == 1 and d.values()["admin_password"] is None


def test_change_folder_dialog_rules(qapp, tmp_path):
    cur = tmp_path / DATA_SUBDIR
    cur.mkdir()
    d = ChangeFolderDialog(cur)
    d.folder.edit.setText(str(tmp_path))                                        # equivale alla cartella in uso
    d._accept()
    assert "gia' la cartella in uso" in d.error.text()
    d.folder.edit.setText(str(cur / "sub"))
    d._accept()
    assert "dentro" in d.error.text()
    d.folder.edit.setText(str(tmp_path / "nuova"))
    d._accept()
    assert d.result() == 1 and d.values() == (str(tmp_path / "nuova"), True)


# ======================================================================================= finestra
def make_window(tmp_path, base=None, **kw):
    qs = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    prefs = ServerPrefs(qs)
    prefs.data_base = str(base or (tmp_path / "dati"))
    prefs.host, prefs.port, prefs.autostart = "127.0.0.1", free_port(), kw.pop("autostart", False)
    w = ServerWindow(prefs, run=sync, confirm_close=False, **kw)
    w._pending_admin = ("admin", ADMIN_PW)
    return w


def test_window_users_stats_and_audit(qapp, tmp_path):
    w = make_window(tmp_path, autostart=True)
    assert w.start() and w.server.running
    w.show()
    assert w.status.led.state == "online" and "in esecuzione" in w.windowTitle()
    w.server.console.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer"))
    w.refresh_stats()
    assert w.status.st_users[1].text() == "2" and w.status.recent.rowCount() >= 2
    w._nav("users")
    assert w.users.users_table.rowCount() == 2
    w._nav("audit")
    assert w.audit.table.rowCount() >= 3 and w.audit.scope.isHidden() and not w.audit.btn_verify.isHidden()
    w.stop_server()
    assert not w.server.running and w.status.btn_start.isEnabled() and w.status.led.state == "offline"
    w.close()


def test_window_change_folder_with_copy_keeps_users_and_restarts(qapp, tmp_path, monkeypatch):
    w = make_window(tmp_path, autostart=True)
    assert w.start()
    w.server.console.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer"))
    old_dir = w.server.data_dir
    new_base = tmp_path / "altro_disco"
    assert w.apply_data_folder(str(new_base), copy=True)
    assert w.server.data_dir == new_base / DATA_SUBDIR and has_data(new_base / DATA_SUBDIR)
    assert {u.username for u in w.server.console.users()} == {"admin", "mario"}        # dati copiati
    assert w.server.running                                                            # riavviato da solo
    assert w.prefs.data_base == str(new_base) and has_data(old_dir)                    # la vecchia cartella resta
    w.server.console.create_user(UserCreate(username="nuovo", display_name="N", password=PW, role="Viewer"))
    assert len(w.server.console.users()) == 3
    w.close()


def test_window_change_folder_without_copy_starts_fresh(qapp, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    w = make_window(tmp_path)
    assert w.start() and not w.server.running                                           # autostart disattivato
    w.server.console.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer"))
    assert w.apply_data_folder(str(tmp_path / "vuota"), copy=False)
    assert [u.username for u in w.server.console.users()] == ["admin"]
    assert shown and "Password:" in shown[-1]                                           # nuova password mostrata
    assert not w.server.running                                                         # era fermo: resta fermo
    w.close()


def test_window_reuses_existing_folder_data(qapp, tmp_path):
    w1 = make_window(tmp_path)
    w1.start()
    w1.server.console.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer"))
    w1.close()
    w2 = make_window(tmp_path, base=w1.prefs.data_base)
    assert w2.start() is True
    assert {u.username for u in w2.server.console.users()} == {"admin", "mario"}
    w2.close()


def test_window_network_change_restarts_on_new_port(qapp, tmp_path):
    w = make_window(tmp_path, autostart=True)
    assert w.start()
    port = free_port()
    w.apply_network("127.0.0.1", port)
    assert w.server.running and w.server.port == port and w.prefs.port == port
    assert httpx.get(f"http://127.0.0.1:{port}/api/health").status_code == 200
    w.close()


def test_window_start_error_is_reported(qapp, tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w = make_window(tmp_path)
    w.start()
    busy = socket.socket()
    busy.bind(("127.0.0.1", w.server.port))
    busy.listen(1)
    try:
        w.start_server()
        assert warned and "porta" in warned[-1].lower() and not w.server.running
        assert w.status.btn_start.isEnabled()
    finally:
        busy.close()
        w.close()


def test_window_can_shrink_below_content_and_pages_scroll(qapp, tmp_path):
    """Regressione: la finestra non deve avere un minimo piu' alto dello schermo (le pagine lunghe scorrono)."""
    w = make_window(tmp_path)
    assert w.start()
    w.show()
    hint = w.minimumSizeHint()
    assert hint.height() <= 600 and hint.width() <= 1000, hint
    w.resize(900, 560)                                                # ridimensionabile
    assert w.height() <= 600
    w._nav("settings")
    assert w.pages.currentWidget() is w.settings_page
    w.close()
