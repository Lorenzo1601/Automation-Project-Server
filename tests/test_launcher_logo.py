"""Test dell'exe unico (launcher) e del logo dell'applicazione."""
import os
import struct
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from server import launcher                                                  # noqa: E402
from server.service_ctl import last_command_log                               # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "client" / "theme" / "assets"


# ======================================================================================= launcher
@pytest.fixture
def routes(monkeypatch):
    calls = []
    import server.__main__ as srv_main
    import server.gui.app as gui_app
    import server.service as service
    monkeypatch.setattr(gui_app, "main", lambda: calls.append(("gui",)))
    monkeypatch.setattr(service, "main", lambda a: calls.append(("service", a)) or 0)
    monkeypatch.setattr(service, "host_service", lambda: calls.append(("host",)))
    monkeypatch.setattr(srv_main, "main", lambda: calls.append(("serve", list(sys.argv[1:]))))
    monkeypatch.setattr(sys, "argv", ["AutomationProjectServer.exe"])
    return calls


def test_no_arguments_opens_the_gui(routes):
    assert launcher.main([]) == 0 and routes == [("gui",)]
    routes.clear()
    assert launcher.main(["gui"]) == 0 and routes == [("gui",)]


@pytest.mark.parametrize("cmd", ["install", "start", "stop", "restart", "status", "remove", "run"])
def test_service_commands_are_routed_to_service_module(routes, cmd):
    assert launcher.main([cmd, "--data-dir", "D:\\APS"]) == 0
    assert routes == [("service", [cmd, "--data-dir", "D:\\APS"])]


def test_serve_runs_headless_server_with_its_own_options(routes):
    assert launcher.main(["serve", "--port", "9000", "--seed-project", "Linea 1"]) == 0
    assert routes == [("serve", ["--port", "9000", "--seed-project", "Linea 1"])]


def test_run_service_hands_control_to_windows_service_manager(routes):
    assert launcher.main(["--run-service"]) == 0 and routes == [("host",)]


def test_help_and_unknown_commands(routes, capsys):
    assert launcher.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "applicazione grafica" in out and "install" in out and "serve" in out
    assert launcher.main(["boh"]) == 2 and "Automation Project Server" in capsys.readouterr().out
    assert routes == []


def test_without_console_output_goes_to_log_file(routes, monkeypatch, tmp_path):
    """Exe 'windowed' senza console (UAC, servizio): sys.stdout e' None e l'esito va nel file di log."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert launcher.main(["boh"]) == 2
    sys.stdout.flush()
    assert "applicazione grafica" in last_command_log().read_text("utf-8")
    assert last_command_log().parent == tmp_path


def test_entry_points_are_thin_shims():
    root = Path(__file__).resolve().parent.parent
    for f in ("run_server.py", "run_server_gui.py", "run_server_service.py"):
        text = (root / f).read_text("utf-8")
        assert "server.launcher" in text and len(text.splitlines()) < 15


# ======================================================================================= logo
def test_logo_assets_are_valid():
    png = ASSETS / "logo.png"
    ico = ASSETS / "app.ico"
    head = png.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", head[16:24])
    assert (w, h) == (512, 512)
    data = ico.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 5
    sizes = {data[6 + 16 * i] or 256 for i in range(count)}               # larghezze nelle voci della directory
    assert {16, 32, 48, 256} <= sizes


def test_logo_helpers_and_views_use_it():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from client.theme import apply_dark_theme
    from client.theme.widgets import LOGO_PATH, app_icon, logo_pixmap
    apply_dark_theme(app)
    assert LOGO_PATH == ASSETS / "logo.png" and LOGO_PATH.exists()
    pm = logo_pixmap(84)
    assert not pm.isNull() and pm.devicePixelRatio() == 2 and pm.width() == 168
    assert not app_icon().isNull()
    from client.views.login_view import LoginView
    from client.views.sidebar import Sidebar
    sync = lambda fn, ok=None, err=None: ok and ok(fn())                   # noqa: E731
    v = LoginView(run=sync, discover=lambda: [])
    sb = Sidebar()
    from PySide6.QtWidgets import QLabel
    assert any(l.pixmap() and l.pixmap().width() == 84 for l in v.findChildren(QLabel))      # logo nel pannello del login
    assert any(l.pixmap() and l.pixmap().width() == 36 for l in sb.findChildren(QLabel))     # logo nella sidebar
    v.close()
    sb.close()
