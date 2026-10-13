"""Controller del server per la GUI (senza dipendenze Qt, quindi testabile).

``EmbeddedServer`` possiede l'applicazione FastAPI (database + progetti) e il server uvicorn, avviato in un thread.
L'applicazione esiste anche a server fermo: la console amministrativa (``ConsoleApi``) la interroga *in-process*,
quindi utenti e permessi si gestiscono sempre, anche quando nessun client puo' collegarsi.
"""
from __future__ import annotations

import logging
import shutil
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import uvicorn

from client.api_client import ApiClient, ApiError
from common.discovery import DISCOVERY_PORT
from client import trust as _trust
from server import tls as tlsmod
from server.config import Settings
from server.discovery import DiscoveryResponder
from server.main import create_app

DATA_SUBDIR = "AutomationProjectServer"
DB_NAME = "aps.db"


# ------------------------------------------------------------------------------------------ cartella dati
def resolve_data_dir(base: "str | Path") -> Path:
    """Cartella effettiva dei dati a partire da quella scelta dall'utente.

    Il server crea *dentro* la cartella scelta una sottocartella ``AutomationProjectServer`` (database, progetti,
    revisioni). Se la cartella scelta e' gia' una cartella dati (contiene ``aps.db`` o si chiama gia' cosi') viene
    usata cosi' com'e', per non annidare cartelle a ogni cambio.
    """
    p = Path(base).expanduser()
    if (p / DB_NAME).exists() or p.name == DATA_SUBDIR:
        return p
    return p / DATA_SUBDIR


def has_data(data_dir: "str | Path") -> bool:
    return (Path(data_dir) / DB_NAME).exists()


def check_writable(folder: "str | Path") -> Optional[str]:
    """Ritorna un messaggio d'errore se non si puo' scrivere in ``folder`` (o crearla), altrimenti None."""
    p = Path(folder).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f".aps_probe_{time.time_ns()}"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return f"Impossibile scrivere in '{p}': {exc.strerror or exc}"
    return None


def copy_data(src: "str | Path", dst: "str | Path") -> int:
    """Copia l'intera cartella dati (a server/DB chiusi). Ritorna il numero di file copiati."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        return 0
    if dst.resolve().is_relative_to(src.resolve()):
        raise ValueError("La nuova cartella non puo' trovarsi dentro quella attuale")
    n = 0
    for f in src.rglob("*"):
        if f.is_file() and not f.name.endswith(("-wal", "-shm")):
            t = dst / f.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)
            n += 1
    return n


def local_addresses() -> list[str]:
    """Indirizzi IPv4 della macchina con cui i client possono raggiungere il server."""
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except OSError:
        pass
    try:                                                      # interfaccia di uscita (non invia pacchetti)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip not in out and not ip.startswith("127."):
                out.insert(0, ip)
    except OSError:
        pass
    return out


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("" if host in ("0.0.0.0", "") else host, port))
        except OSError:
            return True
    return False


# ------------------------------------------------------------------------------------------ console API
class ConsoleApi(ApiClient):
    """``ApiClient`` che parla con l'applicazione in-process (nessuna rete) con una sessione amministrativa locale."""

    def __init__(self, app) -> None:                                               # noqa: ANN001
        from starlette.testclient import TestClient
        super().__init__("http://console.local")
        self._http.close()
        self._http = TestClient(app, base_url="http://console.local", raise_server_exceptions=False,
                                headers={"User-Agent": "AutomationProjectServerConsole/2.0"},
                                client=("127.0.0.1", 0))
        self._app = app
        self._inproc = self._http
        self.remote_url: Optional[str] = None
        self.open()

    def use_remote(self, url: Optional[str], pin: Optional[str] = None) -> None:
        """Instrada le chiamate a un server in esecuzione in un altro processo (servizio Windows) sugli stessi dati.

        Il token della console e' firmato con lo stesso segreto e la sessione sta nello stesso database, quindi il
        servizio lo accetta: cosi' creazione di progetti e inizializzazione del Master passano dal servizio e non
        lasciano la sua memoria disallineata dai file.

        ``https://``: se si conosce l'impronta del certificato del servizio (``pin``, letta dalla cartella dati) la
        connessione e' fissata a quella; senza pin la verifica e' disattivata SOLO se l'indirizzo e' il loopback
        (stesso PC, il traffico non esce dalla macchina); altrimenti vale la verifica normale.
        """
        key = (url, pin)
        if url == self.remote_url and key == getattr(self, "_remote_key", None):
            return
        import httpx
        self.remote_url, self._remote_key = url, key
        self.base_url = (url or "http://console.local").rstrip("/")      # serve a riconoscere gli errori di certificato (schema)
        if not url:
            self._http = self._inproc
            return
        kw: dict = {}
        if _trust.is_secure_url(url):
            host, port = _trust.host_port(url)
            if pin:
                kw["verify"] = _trust.PinnedSSLContext(pin, host, port)
            elif _trust.is_loopback(host):
                kw["verify"] = False
            else:
                kw["verify"] = _trust.system_context()
        self._http = httpx.Client(base_url=url, timeout=60.0, **kw)

    def open(self) -> None:
        self._store(self._app.state.auth.console_session())

    def _req(self, method: str, url: str, *, auth: bool = True, **kw):             # noqa: ANN003
        try:
            return super()._req(method, url, auth=auth, **kw)
        except ApiError as exc:
            if exc.status != 401 or not auth:
                raise
            self.open()                  # sessione revocata (es. reset password dell'admin) o scaduta: ne apre una nuova
            return super()._req(method, url, auth=auth, **kw)


# ------------------------------------------------------------------------------------------ log
class LogBuffer(logging.Handler):
    """Raccoglie le righe di log (e le inoltra a un callback) per mostrarle nella GUI."""

    def __init__(self, maxlen: int = 500) -> None:
        super().__init__(logging.INFO)
        self.lines: deque[str] = deque(maxlen=maxlen)
        self.on_line: Optional[Callable[[str], None]] = None
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self.lines.append(line)
        cb = self.on_line
        if cb:
            try:
                cb(line)
            except Exception:                                                       # noqa: BLE001
                pass


# ------------------------------------------------------------------------------------------ server
class EmbeddedServer:
    def __init__(self, data_dir: "str | Path", host: str = "0.0.0.0", port: int = 8750,
                 admin_user: Optional[str] = None, admin_password: Optional[str] = None, name: str = "",
                 discovery_port: int = DISCOVERY_PORT, with_console: bool = True, tls_enabled: bool = False,
                 tls_certfile: str = "", tls_keyfile: str = "") -> None:
        self.tls_enabled, self.tls_certfile, self.tls_keyfile = tls_enabled, tls_certfile, tls_keyfile
        self.tls: Optional[tlsmod.TlsMaterial] = None            # certificato in uso (valido solo a server avviato)
        self.data_dir, self.host, self.port = Path(data_dir), host, port
        self.name, self.discovery_port, self.with_console = name, discovery_port, with_console
        self.discovery: Optional[DiscoveryResponder] = None
        self._admin_user, self._admin_password = admin_user, admin_password
        self.log = LogBuffer()
        self._lg = [logging.getLogger("uvicorn.error"), logging.getLogger("aps")]      # "aps.gui" propaga ad "aps"
        for lg in self._lg:
            lg.setLevel(logging.INFO)
            lg.addHandler(self.log)
        self._glog = logging.getLogger("aps.gui")
        self.app = None
        self.console: Optional[ConsoleApi] = None
        self.bootstrap_password: Optional[str] = None
        self._uv: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self.last_error: str = ""
        self.started_at: Optional[float] = None

    # --------------------------------------------------------------- applicazione
    def open(self) -> None:
        """Crea/apre database e cartelle nella cartella dati; prepara la console. Non mette il server in ascolto."""
        if self.app is not None:
            return
        cfg = Settings(data_dir=self.data_dir, host=self.host, port=self.port, discovery_port=self.discovery_port,
                       tls_enabled=self.tls_enabled, tls_certfile=self.tls_certfile, tls_keyfile=self.tls_keyfile)
        if self.name:
            cfg.server_name = self.name
        if self._admin_user:
            cfg.admin_username = self._admin_user
        if self._admin_password:
            cfg.admin_password = self._admin_password
        self.app = create_app(cfg)
        self.bootstrap_password = self.app.state.bootstrap_password
        if self.with_console:
            self.console = ConsoleApi(self.app)
        self._glog.info("Dati: %s", self.data_dir)

    def close(self) -> None:
        """Ferma il server e rilascia database e file (necessario prima di spostare/copiare la cartella dati)."""
        self.stop()
        if self.console is not None:
            try:
                self.console._http.close()                                           # noqa: SLF001
            except Exception:                                                       # noqa: BLE001
                pass
        if self.app is not None:
            self.app.state.db.dispose()
        self.app = self.console = None

    def detach_logging(self) -> None:
        for lg in self._lg:
            lg.removeHandler(self.log)

    # --------------------------------------------------------------- rete
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._uv and self._uv.started)

    @property
    def starting(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._uv and not self._uv.started)

    @property
    def scheme(self) -> str:
        return tlsmod.scheme_for(self.tls_enabled)

    @property
    def insecure_warning(self) -> Optional[str]:
        """Avviso «connessione non cifrata» se il server e' esposto alla rete senza HTTPS."""
        return tlsmod.insecure_warning(self.host, self.tls_enabled)

    def urls(self) -> list[str]:
        if self.host in ("0.0.0.0", ""):
            hosts = ["localhost", *local_addresses()]
        else:
            hosts = [self.host]
        return [f"{self.scheme}://{h}:{self.port}" for h in hosts]

    def start(self, timeout: float = 15.0) -> bool:
        """Mette il server in ascolto. Ritorna True se e' partito; altrimenti ``last_error`` spiega il motivo."""
        if self.running:
            return True
        self.open()
        self.last_error = ""
        if port_in_use(self.host, self.port):
            self.last_error = f"La porta {self.port} e' gia' in uso da un altro programma (o da un'altra istanza del server)."
            self._glog.error(self.last_error)
            return False
        ssl_kw: dict = {}
        if self.tls_enabled:                                  # con TLS attivo non si serve MAI HTTP in chiaro: se il certificato non va, non si parte
            try:
                self.tls = tlsmod.resolve_tls(self.data_dir, True, self.tls_certfile, self.tls_keyfile)
            except tlsmod.TlsError as exc:
                self.tls = None
                self.last_error = f"HTTPS non attivabile: {exc}"
                self._glog.error(self.last_error)
                return False
            if self.tls.generated:
                self._glog.info("Generato un nuovo certificato autofirmato (%s): i client dovranno accettarlo", self.tls.certfile)
            note = tlsmod.expiry_notice(self.tls.info)
            if note:
                self._glog.warning(note)
            ssl_kw = {"ssl_certfile": self.tls.certfile}
            if self.tls.keyfile != self.tls.certfile:
                ssl_kw["ssl_keyfile"] = self.tls.keyfile
        else:
            self.tls = None
            if self.insecure_warning:
                self._glog.warning(self.insecure_warning)
        # log_config=None: negli eseguibili "windowed" sys.stderr e' None e la configurazione di uvicorn andrebbe in errore
        cfg = uvicorn.Config(self.app, host=self.host, port=self.port, log_config=None, access_log=False, **ssl_kw)
        self._uv = uvicorn.Server(cfg)

        def run() -> None:
            try:
                self._uv.run()
            except SystemExit:
                pass
            except Exception as exc:                                                 # noqa: BLE001
                self.last_error = str(exc)
                self._glog.error("Errore del server: %s", exc)
        self._thread = threading.Thread(target=run, name="aps-uvicorn", daemon=True)
        self._thread.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self._uv.started:
                self.started_at = time.time()
                self._glog.info("Server in ascolto su %s", ", ".join(self.urls()))
                self._start_discovery()
                return True
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        if not self.last_error:
            self.last_error = "Il server non e' riuscito ad avviarsi (vedi il registro)."
        self.stop()
        return False

    def _start_discovery(self) -> None:
        """Annuncia il server in rete (solo se in ascolto su un'interfaccia raggiungibile dagli altri PC)."""
        if self.host.startswith("127.") or self.discovery_port <= 0:
            return
        self.discovery = DiscoveryResponder(self.port, self.app.state.settings.display_name, self.discovery_port,
                                            scheme=self.scheme)
        self.discovery.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self.discovery is not None:
            self.discovery.stop()
            self.discovery = None
        if self._uv is not None and self._thread is not None:
            self._uv.should_exit = True
            self._thread.join(timeout)
            if self._thread.is_alive():
                self._uv.force_exit = True
                self._thread.join(3)
            self._glog.info("Server fermato")
        self._uv = self._thread = None
        self.started_at = None

    def backup(self, dest: "str | Path") -> Path:
        """Crea uno .zip consistente dell'intera cartella dati (database, progetti, segreto JWT), anche a server acceso.

        Il database viene copiato con l'API di backup di SQLite e, finche' dura la copia dei file, i progetti caricati
        sono bloccati (nessun merge a meta' archivio). Il file contiene il segreto JWT: va conservato in un posto protetto
        (vedi ``server.backup``). Registra ``backup_created`` nell'audit (attore ``console``). Ritorna il percorso creato.
        """
        import contextlib
        from server.backup import create_backup
        self.open()
        with contextlib.ExitStack() as stack:
            for rt in sorted(self.app.state.registry.runtimes(), key=lambda r: r.project_id):
                stack.enter_context(rt.mgr._lock)                                    # noqa: SLF001
            info = create_backup(self.data_dir, dest)
        self.app.state.audit.record("backup_created", "console", target=Path(info["path"]).name,
                                    details={"path": str(info["path"]), "bytes": info["bytes"], "files": info["files"]})
        self._glog.info("Backup creato: %s (%d byte)", info["path"], info["bytes"])
        return Path(info["path"])

    def client_update(self):
        """Client attualmente pubblicato per l'aggiornamento automatico (``UpdateInfo``; ``version`` vuota = nessuno)."""
        from server.updates import read_info
        return read_info(self.data_dir)

    def publish_client_update(self, exe: "str | Path", version: str, notes: str = ""):
        """Pubblica ``exe`` come ultima versione del client: i client collegati la proporranno all'avvio."""
        from server.updates import publish
        self.open()
        info = publish(self.data_dir, Path(exe), version, notes, actor="console")
        self.app.state.audit.record("client_update_published", "console", target=info.version,
                                    details={"sha256": info.sha256, "size": info.size, "filename": info.filename})
        self._glog.info("Aggiornamento del client pubblicato: %s", info.version)
        return info

    def remove_client_update(self) -> bool:
        from server.updates import remove
        self.open()
        done = remove(self.data_dir)
        if done:
            self.app.state.audit.record("client_update_removed", "console")
        return done

    def connections(self) -> int:
        return self.app.state.notifier.connection_count() if self.app is not None else 0
