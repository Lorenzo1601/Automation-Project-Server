"""TLS integrato: certificato (generazione/validazione), server HTTPS/WSS reale, fiducia del client con pinning (TOFU),
scoperta con schema, servizio e GUI del server."""
import datetime as dt
import hashlib
import os
import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from cryptography import x509                                              # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization          # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa             # noqa: E402
from cryptography.x509.oid import NameOID                                  # noqa: E402
from PySide6.QtCore import QSettings                                       # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox                    # noqa: E402

from client import trust                                                   # noqa: E402
from client.api_client import ApiClient, ApiError, TlsTrustError          # noqa: E402
from client.discovery import ServerInfo, discover                          # noqa: E402
from client.theme import apply_dark_theme                                  # noqa: E402
from client.ws_listener import WsListener                                  # noqa: E402
from common.discovery import make_reply, parse_reply                       # noqa: E402
from common.schemas import ProjectCreate, UserCreate                       # noqa: E402
from server import service as svc                                          # noqa: E402
from server import tls                                                     # noqa: E402
from server.config import Settings                                         # noqa: E402
from server.discovery import DiscoveryResponder                            # noqa: E402
from server.gui.controller import EmbeddedServer, resolve_data_dir        # noqa: E402
from server.gui.main_window import ServerWindow                            # noqa: E402
from server.gui.settings import ServerPrefs                                # noqa: E402
from server.gui.setup_dialog import SetupDialog                            # noqa: E402
from server.service_ctl import ServiceConfig                               # noqa: E402

ADMIN_PW = "AdminPass123"
PW = "Password12345"


def free_port(udp=False) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def sync(fn, ok=None, err=None):
    try:
        res = fn()
    except Exception as exc:                                               # noqa: BLE001
        if err:
            err(exc)
        return
    if ok:
        ok(res)


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


def make_pair(tmp: Path, name="x", key_type="rsa", days_before=10, days_after=100, cn="prova"):
    """Certificato + chiave PEM arbitrari (anche gia' scaduti) per i test."""
    key = rsa.generate_private_key(65537, 2048) if key_type == "rsa" else ec.generate_private_key(ec.SECP256R1())
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=days_before)).not_valid_after(now + dt.timedelta(days=days_after))
            .sign(key, hashes.SHA256()))
    c, k = tmp / f"{name}.crt", tmp / f"{name}.key"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return c, k


# ======================================================================================= certificato
def test_generate_self_signed_san_validity_and_files(tmp_path):
    info = tls.generate_self_signed(tmp_path, hostnames=["pc-linea1", "localhost"], addresses=["127.0.0.1", "192.168.1.20"])
    cert, key = tls.default_paths(tmp_path)
    assert cert.exists() and key.exists() and cert.parent == tmp_path / "tls"
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"pc-linea1", "localhost"}
    assert {str(i) for i in san.get_values_for_type(x509.IPAddress)} == {"127.0.0.1", "192.168.1.20"}
    assert 824 <= (c.not_valid_after_utc - c.not_valid_before_utc).days <= 826 and 824 <= info.days_left <= 825
    assert info.sha256 == hashlib.sha256(c.public_bytes(serialization.Encoding.DER)).hexdigest()
    assert info.self_signed and info.subject == "pc-linea1" and not info.expired
    assert tls.read_cert_info(cert).sha256 == info.sha256
    assert isinstance(serialization.load_pem_private_key(key.read_bytes(), None), rsa.RSAPrivateKey)
    assert tls.validate_pair(cert, key).sha256 == info.sha256
    if os.name == "posix":
        assert (key.stat().st_mode & 0o077) == 0                              # chiave leggibile solo dal proprietario


def test_generate_default_names_include_hostname_and_loopback(tmp_path):
    info = tls.generate_self_signed(tmp_path)
    assert "localhost" in info.san and "127.0.0.1" in info.san and socket.gethostname() in info.san


def test_generate_ec_and_regenerate_changes_fingerprint(tmp_path):
    a = tls.generate_self_signed(tmp_path, key_type="ec", hostnames=["h"], addresses=["127.0.0.1"])
    assert isinstance(serialization.load_pem_private_key(tls.default_paths(tmp_path)[1].read_bytes(), None),
                      ec.EllipticCurvePrivateKey)
    b = tls.generate_self_signed(tmp_path, hostnames=["h"], addresses=["127.0.0.1"])
    assert a.sha256 != b.sha256 and tls.read_cert_info(tls.default_paths(tmp_path)[0]).sha256 == b.sha256
    with pytest.raises(ValueError):
        tls.generate_self_signed(tmp_path, key_type="dsa")


def test_fingerprint_format_is_shared_by_server_and_client():
    h = hashlib.sha256(b"x").hexdigest()
    assert tls.format_fingerprint(h) == trust.format_fingerprint(h)
    assert trust.format_fingerprint(h).count(":") == 31
    two = trust.format_fingerprint(h, per_line=16).split("\n")
    assert len(two) == 2 and all(x.count(":") == 15 for x in two)


def test_validate_pair_rejects_mismatch_garbage_expired_and_encrypted(tmp_path):
    c1, k1 = make_pair(tmp_path, "a")
    c2, k2 = make_pair(tmp_path, "b")
    assert tls.validate_pair(c1, k1).subject == "prova"
    with pytest.raises(tls.TlsError, match="non corrispondono"):
        tls.validate_pair(c1, k2)
    with pytest.raises(tls.TlsError, match="non trovato"):
        tls.validate_pair(tmp_path / "manca.crt", k1)
    junk = tmp_path / "junk.pem"
    junk.write_text("non sono un certificato")
    with pytest.raises(tls.TlsError, match="PEM"):
        tls.validate_pair(junk, k1)
    with pytest.raises(tls.TlsError, match="chiave privata"):
        tls.validate_pair(c1, junk)
    der = tmp_path / "c.der"
    der.write_bytes(x509.load_pem_x509_certificate(c1.read_bytes()).public_bytes(serialization.Encoding.DER))
    with pytest.raises(tls.TlsError, match="PEM"):
        tls.validate_pair(der, k1)
    old_c, old_k = make_pair(tmp_path, "old", days_before=30, days_after=-1)
    with pytest.raises(tls.TlsError, match="scaduto"):
        tls.validate_pair(old_c, old_k)
    fut_c, fut_k = make_pair(tmp_path, "fut", days_before=-5, days_after=100)
    with pytest.raises(tls.TlsError, match="non e' ancora valido"):
        tls.validate_pair(fut_c, fut_k)
    key = serialization.load_pem_private_key(k1.read_bytes(), None)
    enc = tmp_path / "enc.key"
    enc.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.BestAvailableEncryption(b"segreto")))
    with pytest.raises(tls.TlsError, match="password"):
        tls.validate_pair(c1, enc)


def test_validate_pair_accepts_combined_pem_and_chain(tmp_path):
    c, k = make_pair(tmp_path, "comb", key_type="ec")
    both = tmp_path / "both.pem"
    both.write_bytes(c.read_bytes() + k.read_bytes())
    assert tls.validate_pair(both).subject == "prova"                         # chiave nello stesso file
    c2, _ = make_pair(tmp_path, "inter")
    chain = tmp_path / "chain.pem"
    chain.write_bytes(c.read_bytes() + c2.read_bytes())
    assert tls.validate_pair(chain, k).sha256 == tls.read_cert_info(c).sha256  # conta il primo certificato (foglia)


def test_resolve_tls_modes(tmp_path):
    assert tls.resolve_tls(tmp_path, False) is None
    a = tls.resolve_tls(tmp_path, True)
    assert a.generated and Path(a.certfile).exists() and a.keyfile.endswith("server.key")
    b = tls.resolve_tls(tmp_path, True)
    assert not b.generated and b.info.sha256 == a.info.sha256                 # riusa quello esistente
    # scaduto/danneggiato: ne crea uno nuovo
    cert, key = tls.default_paths(tmp_path)
    oc, ok_ = make_pair(tmp_path, "old", days_before=30, days_after=-1)
    cert.write_bytes(oc.read_bytes())
    key.write_bytes(ok_.read_bytes())
    c = tls.resolve_tls(tmp_path, True)
    assert c.generated and c.info.sha256 != a.info.sha256 and not c.info.expired
    # certificato dell'utente: usato cosi' com'e', errori espliciti
    uc, uk = make_pair(tmp_path, "user")
    u = tls.resolve_tls(tmp_path, True, str(uc), str(uk))
    assert u.certfile == str(uc) and not u.generated
    with pytest.raises(tls.TlsError):
        tls.resolve_tls(tmp_path, True, str(uc), str(tmp_path / "nokey.key"))
    assert tls.insecure_warning("0.0.0.0", False) and not tls.insecure_warning("0.0.0.0", True)
    assert not tls.insecure_warning("127.0.0.1", False)


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("APS_TLS_ENABLED", "true")
    monkeypatch.setenv("APS_TLS_CERTFILE", "/c.pem")
    monkeypatch.setenv("APS_TLS_KEYFILE", "/k.pem")
    s = Settings()
    assert s.tls_enabled and s.tls_certfile == "/c.pem" and s.tls_keyfile == "/k.pem" and s.scheme == "https"
    monkeypatch.delenv("APS_TLS_ENABLED")
    assert not Settings().tls_enabled and Settings().scheme == "http"


# ======================================================================================= fiducia (parte pura)
def test_trust_helpers_and_store(tmp_path):
    assert trust.host_port("https://Server:8750") == ("server", 8750)
    assert trust.host_port("wss://a.b/ws?x=1") == ("a.b", 443)
    assert trust.host_port("http://a.b") == ("a.b", 80)
    assert trust.server_key("Srv", 1) == "srv:1" and trust.is_secure_url("wss://x") and not trust.is_secure_url("http://x")
    fp = hashlib.sha256(b"abc").hexdigest()
    assert trust.normalize_fp(trust.format_fingerprint(fp)) == fp and trust.normalize_fp("zz") == ""
    st = trust.TrustStore(tmp_path / "cfg" / "t.json")
    assert st.get("h", 1) is None and not st.remove("h", 1)
    st.pin("H", 8750, fp.upper(), trust.CertSummary(sha256=fp, subject="x"), der=b"non-coerente")
    assert st.get("h", 8750) == fp and trust.TrustStore(st.path).get("h", 8750) == fp   # persistente
    assert st.get_der("h", 8750) is None                                                 # DER non coerente: ignorato
    assert st.get("h", 8751) is None
    with pytest.raises(ValueError):
        st.pin("h", 1, "1234")
    assert st.remove("h", 8750) and st.get("h", 8750) is None
    st.path.write_text("{non json")
    assert st.all() == {}                                                                # file danneggiato: nessun pin, nessun crash


# ======================================================================================= server reale in HTTPS
@pytest.fixture(scope="module")
def tls_live(tmp_path_factory):
    srv = EmbeddedServer(resolve_data_dir(tmp_path_factory.mktemp("tls")), "127.0.0.1", free_port(), admin_password=ADMIN_PW,
                         discovery_port=0, tls_enabled=True)
    srv.open()
    u = srv.console.create_user(UserCreate(username="mario", display_name="Mario", password=PW, role="Developer",
                                            must_change_password=False))
    p = srv.console.create_project(ProjectCreate(name="Linea TLS"))
    srv.console.set_permission(p.id, u.id, "Developer")
    assert srv.start(), srv.last_error
    yield {"srv": srv, "url": f"https://127.0.0.1:{srv.port}", "port": srv.port, "pid": p.id,
           "sha": srv.tls.info.sha256}
    srv.close()
    srv.detach_logging()


def store_for(tmp_path) -> trust.TrustStore:
    return trust.TrustStore(tmp_path / "trusted.json")


def test_server_uses_https_urls_and_never_plain_http(tls_live):
    srv = tls_live["srv"]
    assert srv.running and srv.scheme == "https" and all(u.startswith("https://") for u in srv.urls())
    assert tls_live["srv"].tls.info.self_signed and Path(srv.tls.certfile).parent.name == "tls"
    # niente HTTP in chiaro sulla stessa porta: ne' via httpx, ne' con una richiesta grezza
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{srv.port}/api/health", timeout=3)
    with socket.create_connection(("127.0.0.1", srv.port), timeout=3) as s:
        s.sendall(b"GET /api/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        s.settimeout(3)
        try:
            data = s.recv(4096)
        except (socket.timeout, ConnectionError):
            data = b""
    assert b"HTTP/1." not in data and b"status" not in data
    # su HTTPS risponde (qui il test non verifica il certificato: e' un controllo del server, non del client)
    assert httpx.get(tls_live["url"] + "/api/health", verify=False).status_code == 200


def test_client_refuses_unknown_self_signed_certificate_without_pin(tls_live, tmp_path):
    st = store_for(tmp_path)
    ctx = trust.context_for(tls_live["url"], st)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname                 # mai verifica disattivata senza pin
    api = ApiClient(tls_live["url"], trust=st)
    assert api.secure and api.health() is False
    assert isinstance(api.tls_issue, TlsTrustError) and api.tls_issue.kind == "untrusted"
    with pytest.raises(TlsTrustError) as e:
        api.login("mario", PW)
    assert e.value.kind == "untrusted" and e.value.host == "127.0.0.1" and e.value.port == tls_live["port"]
    assert "autofirmato" in e.value.detail and e.value.status == 0 and isinstance(e.value, ApiError)


def test_first_use_pin_then_ok_without_asking_then_changed_is_rejected(tls_live, tmp_path):
    st = store_for(tmp_path)
    host, port = "127.0.0.1", tls_live["port"]
    asked = []

    def accept(summary):
        asked.append(summary)
        return True
    assert trust.ensure_trusted(host, port, st, accept) is True
    s = asked[0]
    assert s.sha256 == tls_live["sha"] and s.self_signed and not s.expired and "127.0.0.1" in s.san
    assert s.subject == tls_live["srv"].tls.info.subject and "dal " in s.validity_text()
    assert st.get(host, port) == tls_live["sha"] and st.get_der(host, port) is not None
    # connessioni successive: nessuna domanda, tutto funziona (anche con 'localhost', stesso pin per host:porta distinto)
    api = ApiClient(tls_live["url"], trust=st)
    assert api.health() and api.tls_issue is None
    api.login("mario", PW)
    assert api.me().username == "mario"
    # rifiuto dell'utente: nessun pin salvato
    other = store_for(tmp_path / "altro")
    assert trust.ensure_trusted(host, port, other, lambda _s: False) is False and other.get(host, port) is None
    # il pin non coincide piu' (certificato rigenerato / attacco): errore specifico, nessun dato inviato
    bad = hashlib.sha256(b"un altro certificato").hexdigest()
    st.pin(host, port, bad)
    api2 = ApiClient(tls_live["url"], trust=st)
    with pytest.raises(TlsTrustError) as e:
        api2.login("mario", PW)
    err = e.value
    assert err.kind == "changed" and err.expected == bad and err.actual == tls_live["sha"]
    assert "CAMBIATO" in err.detail and "attacco" in err.detail and trust.format_fingerprint(bad) in err.detail
    assert api2.health() is False and api2.tls_issue.kind == "changed"
    # rimozione del pin: si torna alla prima connessione (nuova richiesta di fiducia)
    assert st.remove(host, port)
    with pytest.raises(TlsTrustError) as e2:
        ApiClient(tls_live["url"], trust=st).login("mario", PW)
    assert e2.value.kind == "untrusted"
    assert trust.ensure_trusted(host, port, st, accept) and len(asked) == 2
    ApiClient(tls_live["url"], trust=st).login("mario", PW)


def test_fetch_server_cert_matches_server_certificate(tls_live):
    der = trust.fetch_server_cert("127.0.0.1", tls_live["port"])
    assert trust.fingerprint_der(der) == tls_live["sha"] == tls_live["srv"].tls.info.sha256
    assert trust.describe_cert(der).not_after


def test_http_client_against_https_server_gets_a_hint_and_https_against_plain_is_explained(tls_live, tmp_path):
    with pytest.raises(ApiError) as e:
        ApiClient(f"http://127.0.0.1:{tls_live['port']}").login("mario", PW)
    assert "https://" in e.value.detail
    # server in chiaro raggiunto con https://: messaggio chiaro, non un'eccezione SSL grezza
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)

    def serve():
        c, _ = ls.accept()
        c.recv(2048)
        c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        time.sleep(0.2)
        c.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    with pytest.raises(ApiError) as e2:
        ApiClient(f"https://127.0.0.1:{ls.getsockname()[1]}", trust=store_for(tmp_path)).login("mario", PW)
    assert "non risponde in HTTPS" in e2.value.detail and not isinstance(e2.value, TlsTrustError)
    t.join(2)
    ls.close()


# ======================================================================================= WSS
def _wait(qapp, cond, secs=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs and not cond():
        qapp.processEvents()
        time.sleep(0.02)
    return cond()


def _logged(tls_live, st):
    api = ApiClient(tls_live["url"], trust=st)
    api.login("mario", PW)
    api.set_project(tls_live["pid"])
    return api


def test_wss_event_received_with_pinned_certificate(qapp, tls_live, tmp_path):
    st = store_for(tmp_path)
    assert trust.ensure_trusted("127.0.0.1", tls_live["port"], st, lambda _s: True)
    api = _logged(tls_live, st)
    assert api.ws_url().startswith("wss://")
    ws = WsListener()
    ws.trust = st
    got = []
    ws.event_received.connect(got.append)
    ws.start(api.ws_url)
    try:
        assert _wait(qapp, lambda: any(e.get("type") == "hello" for e in got)), ws.last_ssl_error
        assert ws.is_up and tls_live["srv"].connections() >= 1
    finally:
        ws.stop()


def test_wss_refused_without_pin_and_with_wrong_certificate(qapp, tls_live, tmp_path):
    st = store_for(tmp_path)
    trust.ensure_trusted("127.0.0.1", tls_live["port"], st, lambda _s: True)
    api = _logged(tls_live, st)
    # senza pin: il certificato autofirmato non e' attendibile -> nessuna connessione
    ws = WsListener()
    ws.trust = store_for(tmp_path / "vuoto")
    got = []
    ws.event_received.connect(got.append)
    ws.start(api.ws_url)
    assert not _wait(qapp, lambda: bool(got), 2.5) and not ws.is_up and ws.last_ssl_error
    ws.stop()
    # pin di un ALTRO certificato (anche con il suo DER): errori non ignorati
    other = tmp_path / "altro"
    other.mkdir()
    c, _k = make_pair(other, "o", cn="127.0.0.1")
    der = x509.load_pem_x509_certificate(c.read_bytes()).public_bytes(serialization.Encoding.DER)
    st2 = store_for(tmp_path / "st2")
    st2.pin("127.0.0.1", tls_live["port"], trust.fingerprint_der(der), None, der)
    ws2 = WsListener()
    ws2.trust = st2
    ws2.event_received.connect(got.append)
    ws2.start(api.ws_url)
    assert not _wait(qapp, lambda: bool(got), 2.5) and not ws2.is_up and "CAMBIATO" in ws2.last_ssl_error
    ws2.stop()


# ======================================================================================= GUI client (login)
def _client_window(tmp_path, name="c"):
    from client.gui_main import MainWindow
    st = QSettings(str(tmp_path / f"{name}.ini"), QSettings.IniFormat)
    st.setValue("workspace", str(tmp_path / "ws"))
    win = MainWindow(run=sync, settings=st, use_tray=False, heartbeat_ms=60_000)
    win.trust = store_for(tmp_path / name)
    win.login.trust = win.ws.trust = win.trust
    win.pick_folder = lambda _p, _t, _s: str(tmp_path / f"progetto_{name}")
    return win


def test_login_label_shows_encryption_state(qapp, tls_live, tmp_path):
    from client.views.login_view import LoginView
    v = LoginView(servers=["http://localhost:8750"], run=sync, discover=lambda: [])
    assert v.lbl_crypt.text() == "Connessione non cifrata" and v.ico_crypt.isHidden()
    v.server.setEditText("https://server:8750")
    assert v.lbl_crypt.text() == "Connessione cifrata" and not v.ico_crypt.isHidden()
    v.server.setEditText("")
    assert v.lbl_crypt.text() == ""
    # certificato autofirmato non ancora accettato: lo segnala gia' alla verifica del server
    v.trust = store_for(tmp_path)
    v.server.setEditText(tls_live["url"])
    v.check_server()
    assert "da confermare" in v.lbl_crypt.text() and "certificato" in v.lbl_server.text().lower()
    trust.ensure_trusted("127.0.0.1", tls_live["port"], v.trust, lambda _s: True)
    v.check_server()
    assert v.lbl_crypt.text() == "Connessione cifrata" and v.lbl_server.text() == "Server raggiungibile"
    v.trust.pin("127.0.0.1", tls_live["port"], hashlib.sha256(b"altro").hexdigest())
    v.check_server()
    assert "CAMBIATO" in v.lbl_crypt.text()


def test_main_window_tofu_flow_prompts_once_and_blocks_changed_certificate(qapp, tls_live, tmp_path):
    win = _client_window(tmp_path)
    prompts = []

    def confirm(_parent, host, port, summary, reason):
        prompts.append((host, port, summary.sha256))
        return True
    win.confirm_trust = confirm
    win.show()
    win._do_login(tls_live["url"], "mario", PW)
    assert prompts == [("127.0.0.1", tls_live["port"], tls_live["sha"])]
    assert win.stack.currentWidget() is win.workspace_widget and win.api.secure
    assert win.trust.get("127.0.0.1", tls_live["port"]) == tls_live["sha"]
    win._logout()
    win._do_login(tls_live["url"], "mario", PW)
    assert len(prompts) == 1 and win.stack.currentWidget() is win.workspace_widget       # secondo accesso: nessuna domanda
    win.ws.stop()
    win._logout()
    # certificato cambiato: accesso bloccato, il dialogo propone di rimuovere la fiducia
    win.trust.pin("127.0.0.1", tls_live["port"], hashlib.sha256(b"x").hexdigest())
    seen = []
    win.confirm_forget_pin = lambda _p, host, port, msg: (seen.append(msg), False)[1]
    win._do_login(tls_live["url"], "mario", PW)
    assert win.stack.currentWidget() is win.login and "cambiato" in win.login.error.text() and "CAMBIATO" in seen[0]
    assert win.trust.get("127.0.0.1", tls_live["port"]) is not None                      # l'utente non ha rimosso il pin
    win.confirm_forget_pin = lambda *_a: True
    win._do_login(tls_live["url"], "mario", PW)
    assert win.trust.get("127.0.0.1", tls_live["port"]) is None and "Fiducia rimossa" in win.login.error.text()
    # l'utente rifiuta il nuovo certificato: nessun accesso, nessun pin
    win.confirm_trust = lambda *_a: False
    win._do_login(tls_live["url"], "mario", PW)
    assert win.stack.currentWidget() is win.login and "annullata" in win.login.error.text()
    assert win.trust.get("127.0.0.1", tls_live["port"]) is None
    win.close()


# ======================================================================================= scoperta con schema
def test_discovery_announces_scheme_and_old_replies_default_to_http():
    assert parse_reply(make_reply("A", 8750, "i", scheme="https"))["scheme"] == "https"
    assert parse_reply(make_reply("A", 8750, "i"))["scheme"] == "http"
    old = b'{"app":"AutomationProjectServer","v":"2.0","name":"Vecchio","port":8750,"id":"x"}'   # senza il campo nuovo
    assert parse_reply(old)["scheme"] == "http"
    assert parse_reply(b'{"app":"AutomationProjectServer","port":1,"scheme":"ftp"}')["scheme"] == "http"
    assert ServerInfo("A", "h", 1).url == "http://h:1" and ServerInfo("A", "h", 1, "https").url == "https://h:1"
    assert "HTTPS" in ServerInfo("A", "h", 1, "https").label and "HTTPS" not in ServerInfo("A", "h", 1).label


def test_responder_and_embedded_server_announce_https():
    udp = free_port(udp=True)
    r = DiscoveryResponder(8750, "Sicuro", udp_port=udp, bind="127.0.0.1", scheme="https")
    assert r.start()
    try:
        found = discover(0.6, udp, targets=[("127.0.0.1", udp)])
        assert found == [ServerInfo("Sicuro", "127.0.0.1", 8750, "https")] and found[0].url == "https://127.0.0.1:8750"
    finally:
        r.stop()


def test_embedded_server_with_tls_advertises_https_in_discovery(tmp_path):
    udp, port = free_port(udp=True), free_port()
    srv = EmbeddedServer(resolve_data_dir(tmp_path), "0.0.0.0", port, admin_password=ADMIN_PW, discovery_port=udp,
                         tls_enabled=True)
    try:
        assert srv.start(), srv.last_error
        found = discover(0.8, udp, targets=[("127.0.0.1", udp)])
        assert found and found[0].scheme == "https" and found[0].url == f"https://127.0.0.1:{port}"
        assert all(u.startswith("https://") for u in srv.urls())
    finally:
        srv.close()
        srv.detach_logging()


# ======================================================================================= avvio / console / servizio
def test_no_plain_http_when_certificate_is_unusable(tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("niente")
    port = free_port()
    srv = EmbeddedServer(resolve_data_dir(tmp_path), "127.0.0.1", port, admin_password=ADMIN_PW, discovery_port=0,
                         tls_enabled=True, tls_certfile=str(bad), tls_keyfile=str(bad))
    try:
        assert srv.start() is False and "HTTPS non attivabile" in srv.last_error and not srv.running
        with pytest.raises(OSError):                                                      # nessun ripiego su HTTP: la porta e' chiusa
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    finally:
        srv.close()
        srv.detach_logging()


def test_embedded_server_with_user_certificate(tmp_path):
    c, k = make_pair(tmp_path, "mio", key_type="ec")
    srv = EmbeddedServer(resolve_data_dir(tmp_path / "d"), "127.0.0.1", free_port(), admin_password=ADMIN_PW,
                         discovery_port=0, tls_enabled=True, tls_certfile=str(c), tls_keyfile=str(k))
    try:
        assert srv.start(), srv.last_error
        assert srv.tls.certfile == str(c) and not (Path(srv.data_dir) / "tls").exists()      # nessun autofirmato creato
        assert trust.fingerprint_der(trust.fetch_server_cert("127.0.0.1", srv.port)) == tls.read_cert_info(c).sha256
    finally:
        srv.close()
        srv.detach_logging()


def test_insecure_warning_logged_when_exposed_without_tls(tmp_path):
    srv = EmbeddedServer(resolve_data_dir(tmp_path), "0.0.0.0", free_port(), admin_password=ADMIN_PW, discovery_port=0)
    try:
        assert srv.insecure_warning and "non cifrata" in srv.insecure_warning
        assert srv.start(), srv.last_error
        assert any("non cifrata" in ln for ln in srv.log.lines) and srv.urls()[0].startswith("http://")
    finally:
        srv.close()
        srv.detach_logging()
    assert EmbeddedServer(tmp_path, "127.0.0.1").insecure_warning is None
    assert EmbeddedServer(tmp_path, "0.0.0.0", tls_enabled=True).insecure_warning is None


def test_console_use_remote_https_pinned_loopback_and_wrong_pin(tls_live):
    """La console (stessi dati, altro 'processo') parla col servizio HTTPS: pin se noto, altrimenti verifica spenta solo sul loopback."""
    a = tls_live["srv"]
    b = EmbeddedServer(a.data_dir, "127.0.0.1", free_port(), discovery_port=0)
    b.open()
    try:
        b.console.use_remote(tls_live["url"], tls_live["sha"])
        assert any(u.username == "mario" for u in b.console.users())
        b.console.use_remote(tls_live["url"], None)                                 # loopback: ammesso senza pin
        assert any(u.username == "mario" for u in b.console.users())
        b.console.use_remote(tls_live["url"], hashlib.sha256(b"no").hexdigest())
        with pytest.raises(TlsTrustError) as e:
            b.console.users()
        assert e.value.kind == "changed"
        b.console.use_remote(None)
        assert b.console.users()
    finally:
        b.close()
        b.detach_logging()


def test_service_config_carries_tls_and_runner_uses_it(tmp_path):
    cfg = ServiceConfig(data_dir=str(tmp_path / "d"), host="127.0.0.1", port=free_port(), discovery_port=0,
                        tls_enabled=True)
    p = cfg.save(tmp_path / "service.json")
    back = ServiceConfig.load(p)
    assert back.tls_enabled and back.tls_certfile == "" and back.tls_keyfile == ""
    old = tmp_path / "old.json"                                                      # configurazione di un servizio gia' installato
    old.write_text('{"data_dir": "D:\\\\APS", "host": "0.0.0.0", "port": 8750, "name": "", "discovery_port": 8751}')
    assert ServiceConfig.load(old).tls_enabled is False
    r = svc.ServiceRunner(back)
    try:
        assert r.start(), r.server.last_error
        assert all(u.startswith("https://") for u in r.server.urls())
        assert httpx.get(r.server.urls()[0].replace("localhost", "127.0.0.1") + "/api/health", verify=False).status_code == 200
        assert (Path(back.data_dir) / "tls" / "server.crt").exists()
    finally:
        r.stop()


# ======================================================================================= GUI del server
def make_window(tmp_path, **kw):
    qs = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    prefs = ServerPrefs(qs)
    prefs.data_base = str(tmp_path / "dati")
    prefs.host, prefs.port, prefs.autostart = "127.0.0.1", free_port(), kw.pop("autostart", False)
    w = ServerWindow(prefs, run=sync, confirm_close=False, **kw)
    w._pending_admin = ("admin", ADMIN_PW)
    return w


def test_setup_dialog_https_checkbox_follows_network_choice(qapp, tmp_path):
    d = SetupDialog(base=str(tmp_path))
    assert d.host.currentData() == "0.0.0.0" and d.tls.isChecked() and d.values()["tls"] is True
    d.host.setCurrentIndex(d.host.findData("127.0.0.1"))
    assert not d.tls.isChecked() and d.values()["tls"] is False
    d.tls.click()                                                                    # scelta esplicita dell'utente: non piu' automatica
    assert d.tls.isChecked()
    d.host.setCurrentIndex(d.host.findData("0.0.0.0"))
    d.host.setCurrentIndex(d.host.findData("127.0.0.1"))
    assert d.values()["tls"] is True


def test_window_apply_https_restarts_with_certificate_and_back_to_http(qapp, tmp_path):
    w = make_window(tmp_path, autostart=True)
    assert w.start()
    sp = w.settings_page
    assert not sp.tls_on.isChecked() and w.server.urls()[0].startswith("http://")
    port = free_port()
    w.apply_network("127.0.0.1", port, None, tls={"enabled": True, "mode": "self", "cert": "", "key": ""})
    srv = w.server
    assert srv.running and srv.tls_enabled and srv.port == port and all(u.startswith("https://") for u in srv.urls())
    assert w.prefs.tls_enabled and w.prefs.tls_mode == "self"
    assert (Path(srv.data_dir) / "tls" / "server.crt").exists()
    assert httpx.get(f"https://127.0.0.1:{port}/api/health", verify=False).status_code == 200
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
    # il pannello mostra impronta e scadenza del certificato in uso
    assert sp.tls_on.isChecked() and tls.format_fingerprint(srv.tls.info.sha256) in sp.lbl_cert.text()
    assert "scade il" in sp.lbl_cert.text() and w.status.lbl_urls.text().startswith("https://")
    acts = [e.action for e in srv.console.audit(project_id=None)]
    assert "tls_settings_changed" in acts
    # ritorno a HTTP
    port = free_port()                                                               # (porta nuova: su Linux la vecchia puo' restare in TIME_WAIT)
    w.apply_network("127.0.0.1", port, None, tls=sp.tls_values() | {"enabled": False})
    assert srv.running and not srv.tls_enabled and httpx.get(f"http://127.0.0.1:{port}/api/health").status_code == 200
    w.close()


def test_window_rejects_invalid_custom_certificate_without_stopping_server(qapp, tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w = make_window(tmp_path, autostart=True)
    assert w.start()
    port = w.server.port
    c1, _k1 = make_pair(tmp_path, "a")
    _c2, k2 = make_pair(tmp_path, "b")
    w.apply_network("127.0.0.1", port, None, tls={"enabled": True, "mode": "custom", "cert": str(c1), "key": str(k2)})
    assert warned and "non corrispondono" in warned[-1]
    assert w.server.running and not w.server.tls_enabled and not w.prefs.tls_enabled          # nessuna modifica
    w.apply_network("127.0.0.1", port, None, tls={"enabled": True, "mode": "custom", "cert": "", "key": ""})
    assert "certificato" in warned[-1].lower() and w.server.running
    # certificato valido dell'utente
    uc, uk = make_pair(tmp_path, "ok", key_type="ec")
    w.apply_network("127.0.0.1", port, None, tls={"enabled": True, "mode": "custom", "cert": str(uc), "key": str(uk)})
    assert w.server.running and w.server.tls.certfile == str(uc) and w.prefs.tls_mode == "custom"
    assert w.settings_page.tls_mode_custom.isChecked() and w.settings_page.tls_cert.text() == str(uc)
    w.close()


def test_window_generate_certificate_asks_before_regenerating_and_audits(qapp, tmp_path):
    w = make_window(tmp_path, autostart=False)
    assert w.start()
    srv = w.server
    w.generate_certificate()
    a = tls.read_cert_info(tls.default_paths(srv.data_dir)[0])
    assert tls.format_fingerprint(a.sha256) in w.settings_page.lbl_cert.text()
    w.generate_certificate(confirm=lambda: False)                                     # annullato: stesso certificato
    assert tls.read_cert_info(tls.default_paths(srv.data_dir)[0]).sha256 == a.sha256
    w.generate_certificate(confirm=lambda: True)
    b = tls.read_cert_info(tls.default_paths(srv.data_dir)[0])
    assert b.sha256 != a.sha256 and tls.format_fingerprint(b.sha256) in w.settings_page.lbl_cert.text()
    entries = srv.console.audit(project_id=None, action="tls_certificate_generated")
    assert len(entries) == 2 and entries[0].details["sha256"] in (a.sha256, b.sha256)
    w.close()


def test_settings_page_warns_when_exposed_without_https(qapp):
    from server.gui.main_window import SettingsPage
    sp = SettingsPage()
    sp.load("x", "0.0.0.0", 8750, False)
    assert not sp.lbl_tls_warn.isHidden() and "non cifrata" in sp.lbl_tls_warn.text()
    sp.tls_on.setChecked(True)
    assert sp.lbl_tls_warn.isHidden() and sp.tls_mode_self.isEnabled() and not sp.tls_cert.isEnabled()
    sp.tls_mode_custom.setChecked(True)
    assert sp.tls_cert.isEnabled() and not sp.btn_tls_generate.isEnabled()
    sp.tls_on.setChecked(False)
    sp.host.setCurrentIndex(sp.host.findData("127.0.0.1"))
    assert sp.lbl_tls_warn.isHidden()
