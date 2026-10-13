"""Fiducia nei certificati dei server HTTPS (trust-on-first-use con pinning dell'impronta SHA-256).

Senza dipendenze Qt (testabile). Regole:

* connessione a un server https **senza pin salvato**: verifica normale con la fiducia di sistema (+ certifi);
  se fallisce (es. certificato autofirmato) il chiamante riceve un errore ``untrusted`` e puo' chiedere all'utente
  se considerare attendibile il certificato (``fetch_server_cert`` + ``TrustStore.pin``);
* connessione con **pin salvato** per ``host:porta``: il certificato presentato deve avere esattamente quell'impronta
  (nessun controllo della catena: il pin E' la decisione dell'utente). Se l'impronta cambia la connessione e' rifiutata
  (``PinMismatch``) e va rimosso il pin per accettare il nuovo certificato.

La verifica non viene mai disattivata senza un pin. ``fetch_server_cert`` apre una connessione solo per leggere il
certificato da mostrare nel dialogo: esegue l'handshake, non invia alcun dato e chiude.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

LOOPBACK = ("localhost", "127.0.0.1", "::1")


# ------------------------------------------------------------------------------------------ impronte
def fingerprint_der(der: bytes) -> str:
    """SHA-256 del certificato in DER, esadecimale minuscolo (64 caratteri)."""
    return hashlib.sha256(der).hexdigest()


def normalize_fp(fp: str) -> str:
    """Impronta in forma canonica (64 esadecimali minuscoli) oppure stringa vuota se non valida."""
    h = "".join(c for c in (fp or "") if c in "0123456789abcdefABCDEF").lower()
    return h if len(h) == 64 else ""


def format_fingerprint(fp: str, per_line: int = 0) -> str:
    """``AB:CD:...`` (byte separati da due punti); con ``per_line`` va a capo ogni N byte (es. 16 -> due righe)."""
    h = normalize_fp(fp).upper()
    pairs = [h[i:i + 2] for i in range(0, len(h), 2)]
    if per_line > 0:
        return "\n".join(":".join(pairs[i:i + per_line]) for i in range(0, len(pairs), per_line))
    return ":".join(pairs)


def host_port(url: str) -> tuple[str, int]:
    """Host e porta di un URL http(s)/ws(s) (porta di default 443/80)."""
    u = urlsplit(url if "://" in url else "http://" + url)
    port = u.port or (443 if u.scheme in ("https", "wss") else 80)
    return (u.hostname or "").lower(), port


def server_key(host: str, port: int) -> str:
    h = host.lower()
    return f"[{h}]:{port}" if ":" in h else f"{h}:{port}"


def is_secure_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("https", "wss")


def is_loopback(host: str) -> bool:
    return host.lower() in LOOPBACK or host.startswith("127.")


# ------------------------------------------------------------------------------------------ descrizione certificato
@dataclass
class CertSummary:
    sha256: str
    subject: str = ""
    issuer: str = ""
    not_before: str = ""            # ISO 8601 UTC
    not_after: str = ""
    san: list[str] = field(default_factory=list)
    self_signed: bool = False

    @property
    def fingerprint(self) -> str:
        return format_fingerprint(self.sha256)

    @property
    def expired(self) -> bool:
        try:
            return dt.datetime.fromisoformat(self.not_after) <= dt.datetime.now(dt.timezone.utc)
        except ValueError:
            return False

    def validity_text(self) -> str:
        def fmt(v: str) -> str:
            try:
                return dt.datetime.fromisoformat(v).astimezone().strftime("%d/%m/%Y")
            except ValueError:
                return "?"
        if not self.not_after:
            return "validita' non disponibile"
        return f"dal {fmt(self.not_before)} al {fmt(self.not_after)}" + (" (SCADUTO)" if self.expired else "")


def describe_cert(der: bytes) -> CertSummary:
    """Impronta e dati principali di un certificato DER (con ``cryptography`` se presente, altrimenti solo l'impronta)."""
    s = CertSummary(sha256=fingerprint_der(der))
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        c = x509.load_der_x509_certificate(der)

        def cn(n: "x509.Name") -> str:
            v = n.get_attributes_for_oid(NameOID.COMMON_NAME)
            return v[0].value if v else n.rfc4514_string()
        s.subject, s.issuer = cn(c.subject), cn(c.issuer)
        s.not_before, s.not_after = c.not_valid_before_utc.isoformat(), c.not_valid_after_utc.isoformat()
        s.self_signed = c.subject == c.issuer
        try:
            ext = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            s.san = [str(v) for v in ext.get_values_for_type(x509.DNSName)] + [str(v) for v in ext.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            pass
    except Exception:                                                              # noqa: BLE001
        pass
    return s


# ------------------------------------------------------------------------------------------ archivio dei pin
def default_trust_path() -> Path:
    """File dei server attendibili nella configurazione dell'utente (sovrascrivibile con ``APS_CLIENT_TRUST_FILE``)."""
    env = os.getenv("APS_CLIENT_TRUST_FILE")
    if env:
        return Path(env)
    if sys.platform.startswith("win"):
        base = Path(os.getenv("APPDATA") or Path.home())
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "AutomationProjectServer" / "trusted_servers.json"


class TrustStore:
    """Impronte attendibili per ``host:porta`` salvate in un file JSON (scrittura atomica; thread-safe)."""

    _lock = threading.RLock()

    def __init__(self, path: "str | Path | None" = None) -> None:
        self.path = Path(path) if path else default_trust_path()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".trust-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: v for k, v in self._load().items() if isinstance(v, dict) and normalize_fp(v.get("sha256", ""))}

    def get(self, host: str, port: int) -> Optional[str]:
        """Impronta salvata per il server (64 esadecimali minuscoli) oppure ``None``."""
        rec = self.all().get(server_key(host, port))
        return normalize_fp(rec["sha256"]) if rec else None

    def pin(self, host: str, port: int, fingerprint: str, summary: Optional[CertSummary] = None,
            der: Optional[bytes] = None) -> None:
        fp = normalize_fp(fingerprint)
        if not fp:
            raise ValueError("Impronta non valida")
        rec = {"sha256": fp, "saved": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        if summary is not None:
            rec.update({k: v for k, v in asdict(summary).items() if k != "sha256"})
        if der and fingerprint_der(der) == fp:
            rec["der"] = base64.b64encode(der).decode("ascii")          # per il WebSocket Qt (CA = solo questo certificato)
        with self._lock:
            d = self._load()
            d[server_key(host, port)] = rec
            self._save(d)

    def get_der(self, host: str, port: int) -> Optional[bytes]:
        rec = self.all().get(server_key(host, port))
        try:
            der = base64.b64decode(rec["der"]) if rec and rec.get("der") else None
        except (ValueError, TypeError):
            return None
        return der if der and fingerprint_der(der) == normalize_fp(rec["sha256"]) else None

    def remove(self, host: str, port: int) -> bool:
        with self._lock:
            d = self._load()
            if d.pop(server_key(host, port), None) is None:
                return False
            self._save(d)
            return True


def default_store() -> TrustStore:
    return TrustStore()


# ------------------------------------------------------------------------------------------ SSL
class PinMismatch(ssl.SSLError):
    """Il server ha presentato un certificato con un'impronta diversa da quella salvata."""

    def __init__(self, host: str, port: int, expected: str, actual: str) -> None:
        super().__init__(f"Impronta del certificato cambiata per {host}:{port}")
        self.host, self.port, self.expected, self.actual = host, port, expected, actual


class PinnedSSLContext(ssl.SSLContext):
    """Contesto TLS che accetta SOLO il certificato con l'impronta SHA-256 indicata (nessun controllo della catena).

    Dopo l'handshake confronta l'impronta del certificato del server: se differisce chiude e solleva ``PinMismatch``.
    Vale per i socket bloccanti (httpx/httpcore sincrono, ``fetch_server_cert``).
    """

    def __new__(cls, fingerprint: str, host: str = "", port: int = 0):                 # noqa: D102
        ctx = super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)
        ctx.pin_sha256 = normalize_fp(fingerprint)
        ctx.pin_host, ctx.pin_port = host, port
        if not ctx.pin_sha256:
            raise ValueError("Impronta non valida")
        ctx.check_hostname = False                    # il pin sostituisce nome e catena
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def wrap_socket(self, sock, *args, **kwargs):                                    # noqa: ANN001, ANN002, ANN003
        s = super().wrap_socket(sock, *args, **kwargs)
        der = s.getpeercert(binary_form=True)
        actual = fingerprint_der(der) if der else ""
        if actual != self.pin_sha256:
            try:
                s.close()
            except OSError:
                pass
            raise PinMismatch(self.pin_host, self.pin_port, self.pin_sha256, actual)
        return s


def system_context() -> ssl.SSLContext:
    """Verifica standard: certificati di sistema piu' il pacchetto certifi."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:                                                              # noqa: BLE001
        pass
    return ctx


def context_for(url: str, store: Optional[TrustStore] = None) -> Optional[ssl.SSLContext]:
    """Contesto SSL per l'URL: ``None`` per http; con pin salvato -> contesto con pinning; altrimenti verifica di sistema."""
    if not is_secure_url(url):
        return None
    host, port = host_port(url)
    pin = (store or default_store()).get(host, port)
    return PinnedSSLContext(pin, host, port) if pin else system_context()


def context_for_fingerprint(url: str, fingerprint: str) -> ssl.SSLContext:
    host, port = host_port(url)
    return PinnedSSLContext(fingerprint, host, port)


def fetch_server_cert(host: str, port: int, timeout: float = 6.0) -> bytes:
    """Certificato (DER) presentato dal server, SOLO per mostrarlo all'utente; non invia alcun dato applicativo."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                   # solo lettura del certificato: nessuna richiesta viene inviata
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host if not _is_ip(host) else None) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("Il server non ha presentato alcun certificato")
    return der


def _is_ip(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------------------------------ messaggi
def untrusted_message(host: str, port: int, reason: str = "") -> str:
    return (f"Il certificato di {host}:{port} non e' attendibile"
            + (f" ({reason})" if reason else "")
            + ". Se conosci questo server puoi verificarne l'impronta e considerarlo attendibile.")


def changed_message(host: str, port: int, expected: str = "", actual: str = "") -> str:
    msg = (f"ATTENZIONE: il certificato di {host}:{port} e' CAMBIATO rispetto a quello che avevi accettato. "
           "Potrebbe trattarsi di un attacco (qualcuno si sta inserendo tra te e il server) oppure l'amministratore "
           "ha rigenerato il certificato. Non inserire la password finche' non hai verificato con l'amministratore.")
    if expected and actual:
        msg += f"\nImpronta salvata:   {format_fingerprint(expected)}\nImpronta ricevuta: {format_fingerprint(actual)}"
    return msg


def wrong_protocol_message(url: str) -> str:
    return (f"Il server a {url} non risponde in HTTPS. Controlla che l'indirizzo e lo schema (http:// o https://) "
            "siano quelli indicati dall'amministratore.")


# ------------------------------------------------------------------------------------------ orchestrazione (pura)
def ensure_trusted(host: str, port: int, store: TrustStore, confirm: Callable[[CertSummary], bool],
                   fetch: Callable[[str, int], bytes] = fetch_server_cert) -> bool:
    """Prima connessione a un certificato non verificabile: legge il certificato, chiede conferma e salva il pin.

    ``confirm(summary)`` e' l'hook (dialogo nella GUI, funzione di test nei test). Ritorna True se il pin e' stato salvato.
    """
    der = fetch(host, port)
    summary = describe_cert(der)
    if not confirm(summary):
        return False
    store.pin(host, port, summary.sha256, summary, der)
    return True
