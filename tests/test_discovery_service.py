"""Test della scoperta automatica del server in LAN e del servizio (parte portabile: configurazione e ServiceRunner)."""
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client.discovery import ServerInfo, discover, local_ipv4                       # noqa: E402
from common.discovery import PROBE, make_reply, parse_reply             # noqa: E402
from server import service as svc                                       # noqa: E402
from server import service_ctl                                          # noqa: E402
from server.discovery import DiscoveryResponder                         # noqa: E402
from server.gui.controller import EmbeddedServer                        # noqa: E402

ADMIN_PW = "AdminPass123"


def free_port(udp=False) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ======================================================================================= protocollo
def test_parse_reply_accepts_only_our_servers():
    ok = parse_reply(make_reply("Linea 1", 8750, "abc"))
    assert ok == {"name": "Linea 1", "port": 8750, "version": "2.0", "id": "abc"}
    for junk in (b"", b"ciao", b"[]", b'{"app":"altro","port":1}', b'{"app":"AutomationProjectServer"}',
                 b'{"app":"AutomationProjectServer","port":0}', b'{"app":"AutomationProjectServer","port":"x"}',
                 b'{"app":"AutomationProjectServer","port":99999}', b"\xff\xfe"):
        assert parse_reply(junk) is None


def test_responder_answers_probe_and_client_lists_servers():
    udp = free_port(udp=True)
    a = DiscoveryResponder(8750, "Pressa", udp_port=udp, bind="127.0.0.1")
    assert a.start()
    try:
        found = discover(0.6, udp, targets=[("127.0.0.1", udp)])
        assert found == [ServerInfo("Pressa", "127.0.0.1", 8750)]
        assert found[0].url == "http://127.0.0.1:8750" and "Pressa" in found[0].label
        # pacchetti estranei vengono ignorati e non producono risposte
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.sendto(b"altra-cosa", ("127.0.0.1", udp))
        with pytest.raises(socket.timeout):
            s.recvfrom(100)
        s.close()
    finally:
        a.stop()
    assert not a.running
    assert discover(0.4, udp, targets=[("127.0.0.1", udp)]) == []


def test_two_servers_are_listed_and_deduplicated():
    u1, u2 = free_port(udp=True), free_port(udp=True)
    r1 = DiscoveryResponder(8750, "Zeta", udp_port=u1, bind="127.0.0.1")
    r2 = DiscoveryResponder(9000, "Alfa", udp_port=u2, bind="127.0.0.1")
    assert r1.start() and r2.start()
    try:
        found = discover(0.6, u1, targets=[("127.0.0.1", u1), ("127.0.0.1", u2), ("127.0.0.1", u1)])
        assert [f.name for f in found] == ["Alfa", "Zeta"] and {f.port for f in found} == {8750, 9000}
    finally:
        r1.stop()
        r2.stop()


def test_same_server_answering_from_two_addresses_is_listed_once():
    udp = free_port(udp=True)
    r = DiscoveryResponder(8750, "Pressa", udp_port=udp, bind="")          # in ascolto su tutte le interfacce
    assert r.start()
    try:
        ips = [ip for ip in local_ipv4()][:1]
        targets = [("127.0.0.1", udp)] + [(ip, udp) for ip in ips]
        found = discover(0.6, udp, targets=targets)
        assert len(found) == 1 and found[0].name == "Pressa"
        if ips:
            assert found[0].host == ips[0] or found[0].host.startswith("127.")   # preferisce l'indirizzo di rete
    finally:
        r.stop()


def test_disabled_discovery_does_not_bind():
    assert DiscoveryResponder(8750, "x", udp_port=0).start() is False


def test_embedded_server_announces_itself_only_when_listening_on_network(tmp_path):
    udp = free_port(udp=True)
    srv = EmbeddedServer(tmp_path / "a", "0.0.0.0", free_port(), admin_password=ADMIN_PW, name="Linea Test",
                         discovery_port=udp, with_console=False)
    srv.open()
    assert srv.console is None
    try:
        assert srv.start()
        found = discover(0.8, udp)                     # percorso predefinito: prova anche 127.0.0.1
        assert any(f.name == "Linea Test" and f.port == srv.port for f in found)
        srv.stop()
        assert discover(0.4, udp) == []
        local = EmbeddedServer(tmp_path / "b", "127.0.0.1", free_port(), admin_password=ADMIN_PW, discovery_port=udp,
                               with_console=False)
        local.open()
        assert local.start() and local.discovery is None          # solo locale: non si annuncia
        local.close()
    finally:
        srv.close()


# ======================================================================================= login client
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication                              # noqa: E402

from client.theme import apply_dark_theme                               # noqa: E402
from client.views.login_view import LoginView                           # noqa: E402


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


def make_login(found, saved=""):
    v = LoginView(servers=["http://localhost:8750"], run=sync, discover=lambda: found)
    v.prefill(saved, "")
    return v


def test_login_single_server_is_selected_automatically(qapp):
    one = [ServerInfo("Linea 1", "10.0.0.5", 8750)]
    v = make_login(one)
    v.search_servers()
    assert v.server.count() >= 1 and v.server_url() == "http://10.0.0.5:8750"
    assert "Trovato" in v.lbl_server.text() and "Linea 1" in v.server.currentText()


def test_login_multiple_servers_show_a_list_and_keep_last_used(qapp):
    two = [ServerInfo("Linea 1", "10.0.0.5", 8750), ServerInfo("Linea 2", "10.0.0.6", 8750)]
    v = make_login(two)
    v.search_servers()
    labels = [v.server.itemText(i) for i in range(v.server.count())]
    assert any("Linea 1" in t for t in labels) and any("Linea 2" in t for t in labels)
    assert "Trovati 2" in v.lbl_server.text()
    v.server.setCurrentIndex(next(i for i, t in enumerate(labels) if "Linea 2" in t))
    assert v.server_url() == "http://10.0.0.6:8750"
    # al riavvio si riseleziona l'ultimo server usato, se e' ancora in rete
    v2 = make_login(two, saved="http://10.0.0.6:8750")
    v2.search_servers()
    assert v2.server_url() == "http://10.0.0.6:8750"


def test_login_no_server_found_keeps_manual_address(qapp):
    v = make_login([], saved="http://192.168.1.50:8750")
    v.search_servers()
    assert v.server_url() == "http://192.168.1.50:8750" and "Nessun server" in v.lbl_server.text()
    v.server.setEditText("http://altro:9000")
    assert v.server_url() == "http://altro:9000"                       # l'indirizzo scritto a mano resta valido


def test_login_discovery_error_does_not_break_login(qapp):
    def boom():
        raise OSError("rete non disponibile")
    v = LoginView(servers=["http://localhost:8750"], run=sync, discover=boom)
    v.search_servers()
    assert v.server_url() == "http://localhost:8750" and v.btn_search.isEnabled()


# ======================================================================================= servizio (parte portabile)
def test_service_config_roundtrip_and_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("APS_SERVICE_CONFIG", str(tmp_path / "cfg" / "service.json"))
    assert service_ctl.config_path() == tmp_path / "cfg" / "service.json"
    with pytest.raises(FileNotFoundError):
        service_ctl.ServiceConfig.load()
    cfg = service_ctl.ServiceConfig(data_dir=str(tmp_path / "dati"), port=9001, name="Linea 9")
    cfg.save()
    back = service_ctl.ServiceConfig.load()
    assert back == cfg and back.host == "0.0.0.0"
    (tmp_path / "cfg" / "service.json").write_text('{"data_dir": "x", "campo_futuro": 1}')
    assert service_ctl.ServiceConfig.load().data_dir == "x"                # chiavi sconosciute ignorate


def test_service_runner_serves_logs_and_stops(tmp_path):
    data = tmp_path / "dati"
    # prima installazione: l'admin viene creato dal bootstrap con la password indicata
    boot = EmbeddedServer(data, "127.0.0.1", 1, admin_password=ADMIN_PW, with_console=False)
    boot.open()
    boot.close()
    boot.detach_logging()
    port = free_port()
    cfg = service_ctl.ServiceConfig(data_dir=str(data), host="127.0.0.1", port=port, name="Svc")
    r = svc.ServiceRunner(cfg)
    assert r.start()
    try:
        url = f"http://127.0.0.1:{port}"
        assert httpx.get(url + "/api/health").status_code == 200
        login = httpx.post(url + "/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert login.status_code == 200
        t = threading.Thread(target=r.wait, kwargs={"poll": 0.1}, daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive()                                           # attende finche' non si chiede l'arresto
        r.request_stop()
        t.join(3)
        assert not t.is_alive()
    finally:
        r.stop()
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1)
    log = (data / "logs" / "server.log").read_text("utf-8")
    assert "Avvio del servizio" in log and "in ascolto" in log


def test_service_runner_exits_when_server_dies(tmp_path):
    cfg = service_ctl.ServiceConfig(data_dir=str(tmp_path / "d"), host="127.0.0.1", port=free_port())
    r = svc.ServiceRunner(cfg)
    assert r.start()
    try:
        r.server.stop()                                               # il server cade da solo: wait() deve ritornare
        t = threading.Thread(target=r.wait, kwargs={"poll": 0.1}, daemon=True)
        t.start()
        t.join(3)
        assert not t.is_alive()
    finally:
        r.stop()


def test_service_runner_start_fails_when_port_busy(tmp_path):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        cfg = service_ctl.ServiceConfig(data_dir=str(tmp_path / "d"), host="127.0.0.1", port=busy.getsockname()[1])
        r = svc.ServiceRunner(cfg)
        assert r.start() is False and "porta" in r.server.last_error.lower()
        r.stop()
    finally:
        busy.close()


def test_service_cli_on_non_windows(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("APS_SERVICE_CONFIG", str(tmp_path / "service.json"))
    assert service_ctl.service_status() == "unavailable"
    assert svc.main(["status"]) == 0 and "Non disponibile" in capsys.readouterr().out
    assert svc.main(["install", "--data-dir", str(tmp_path / "x")]) == 1
    assert "solo su Windows" in capsys.readouterr().err
    assert svc.main(["remove"]) == 1
    assert service_ctl.run_elevated("x", "") is False


def test_service_command_builds_arguments_with_spaces():
    file, params, cwd = service_ctl.service_command("install", "--data-dir", "D:\\Mia Cartella", "--name", "")
    assert '"D:\\Mia Cartella"' in params and params.endswith('""') and "server.launcher" in params


# ======================================================================================= GUI + servizio sugli stessi dati
def test_window_routes_console_through_running_service(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from common.schemas import ProjectCreate
    from server.gui import main_window as mw
    from server.gui.settings import ServerPrefs

    data_base = tmp_path / "base"
    prefs = ServerPrefs(QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat))
    prefs.data_base, prefs.host, prefs.port, prefs.autostart = str(data_base), "127.0.0.1", free_port(), False
    w = mw.ServerWindow(prefs, run=sync, confirm_close=False)
    w._pending_admin = ("admin", ADMIN_PW)
    assert w.start()
    # un "servizio" (altro server sullo stesso database) in ascolto su un'altra porta
    sport = free_port()
    service = EmbeddedServer(w.server.data_dir, "127.0.0.1", sport, with_console=False)
    service.open()
    assert service.start()
    try:
        monkeypatch.setattr(mw, "service_status", lambda: "running")
        monkeypatch.setattr(mw.ServiceConfig, "load", classmethod(lambda cls, path=None: cls(
            data_dir=str(w.server.data_dir), host="127.0.0.1", port=sport)))
        w.refresh_service_status()
        assert w.service_active and w.server.console.remote_url == f"http://127.0.0.1:{sport}"
        assert w.status.btn_start.isEnabled() is False and "servizio" in w.status.lbl_state.text()
        p = w.server.console.create_project(ProjectCreate(name="Via Servizio"))
        # il progetto e' stato creato dal servizio: esiste nel suo registro
        assert service.app.state.registry.runtime(p.id) is not None
        monkeypatch.setattr(mw, "service_status", lambda: "stopped")
        w.refresh_service_status()
        assert not w.service_active and w.server.console.remote_url is None and w.server.console.projects()
    finally:
        service.close()
        w.close()
