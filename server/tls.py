"""TLS del server: certificato autofirmato generato in locale oppure certificato/chiave PEM dell'utente.

Senza dipendenze Qt (testabile). Il server usa HTTPS/WSS quando ``tls_enabled`` e' attivo; il certificato e'

* quello indicato in ``tls_certfile`` / ``tls_keyfile`` (PEM, verificati prima dell'avvio), oppure
* quello autofirmato in ``<dati>/tls/server.crt`` + ``server.key``, creato al primo avvio (e rigenerabile).

Se il certificato non e' utilizzabile il server NON parte (nessun ripiego su HTTP in chiaro).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TLS_SUBDIR = "tls"
CERT_NAME = "server.crt"
KEY_NAME = "server.key"
VALID_DAYS = 825                 # limite dei certificati "pubblici" accettati dai sistemi operativi (Apple/Chrome-like)
EXPIRY_WARN_DAYS = 30

INSECURE_MSG = ("Connessione non cifrata: il server e' in ascolto su tutta la rete senza HTTPS, quindi password e dati "
                "viaggiano in chiaro. Attiva \"Usa HTTPS\" nelle Impostazioni (Rete).")


class TlsError(Exception):
    """Errore di configurazione TLS, con messaggio in italiano pronto per l'utente."""


@dataclass
class CertInfo:
    path: str
    sha256: str                                   # impronta del certificato (DER), esadecimale minuscolo
    subject: str
    issuer: str
    not_before: dt.datetime
    not_after: dt.datetime
    san: list[str] = field(default_factory=list)
    self_signed: bool = False

    @property
    def days_left(self) -> int:
        return (self.not_after - dt.datetime.now(dt.timezone.utc)).days

    @property
    def expired(self) -> bool:
        return self.not_after <= dt.datetime.now(dt.timezone.utc)

    @property
    def fingerprint(self) -> str:
        return format_fingerprint(self.sha256)

    def summary(self) -> str:
        left = self.days_left
        when = self.not_after.astimezone().strftime("%d/%m/%Y")
        tail = f"scade il {when} ({left} giorni)" if left >= 0 else f"SCADUTO il {when}"
        return f"Impronta SHA-256: {self.fingerprint}\n{'Autofirmato' if self.self_signed else 'Emesso da ' + self.issuer} - {tail}"


@dataclass
class TlsMaterial:
    certfile: str
    keyfile: str
    info: CertInfo
    generated: bool = False                       # certificato autofirmato appena creato in questa chiamata


def format_fingerprint(sha256_hex: str) -> str:
    h = "".join(c for c in sha256_hex if c in "0123456789abcdefABCDEF").upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def tls_dir(data_dir: "str | Path") -> Path:
    return Path(data_dir) / TLS_SUBDIR


def default_paths(data_dir: "str | Path") -> tuple[Path, Path]:
    d = tls_dir(data_dir)
    return d / CERT_NAME, d / KEY_NAME


def insecure_warning(host: str, tls_enabled: bool) -> Optional[str]:
    """Avviso da mostrare quando il server e' raggiungibile dalla rete senza cifratura."""
    if not tls_enabled and host in ("0.0.0.0", ""):
        return INSECURE_MSG
    return None


# ------------------------------------------------------------------------------------------ lettura
def _name_text(n: x509.Name) -> str:
    cn = n.get_attributes_for_oid(NameOID.COMMON_NAME)
    return cn[0].value if cn else n.rfc4514_string()


def _san_list(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [str(v) for v in ext.get_values_for_type(x509.DNSName)] + [str(v) for v in ext.get_values_for_type(x509.IPAddress)]


def _info(cert: x509.Certificate, path: str) -> CertInfo:
    return CertInfo(path=str(path), sha256=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
                    subject=_name_text(cert.subject), issuer=_name_text(cert.issuer),
                    not_before=cert.not_valid_before_utc, not_after=cert.not_valid_after_utc, san=_san_list(cert),
                    self_signed=cert.subject == cert.issuer)


def _read(path: "str | Path", what: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        raise TlsError(f"{what} non trovato: {path}") from None
    except OSError as exc:
        raise TlsError(f"{what} non leggibile ({path}): {exc.strerror or exc}") from None


def _load_chain(certfile: "str | Path") -> list[x509.Certificate]:
    data = _read(certfile, "File del certificato")
    try:
        chain = x509.load_pem_x509_certificates(data)
    except ValueError:
        chain = []
    if not chain:
        raise TlsError(f"'{Path(certfile).name}' non e' un certificato PEM valido (testo che inizia con "
                       "-----BEGIN CERTIFICATE-----). Se e' in formato DER/PFX convertilo in PEM.")
    return chain


def read_cert_info(certfile: "str | Path") -> CertInfo:
    """Informazioni sul (primo) certificato di un file PEM; solleva ``TlsError`` se illeggibile."""
    return _info(_load_chain(certfile)[0], str(certfile))


def _load_key(keyfile: "str | Path"):
    data = _read(keyfile, "File della chiave privata")
    try:
        return serialization.load_pem_private_key(data, password=None)
    except TypeError:
        raise TlsError("La chiave privata e' protetta da password: non e' supportata. Esportala senza password "
                       "(es. openssl rsa -in chiave.key -out chiave-nopass.key).") from None
    except Exception as exc:                                                       # noqa: BLE001
        raise TlsError(f"'{Path(keyfile).name}' non e' una chiave privata PEM valida: {exc}") from None


def _spki(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def validate_pair(certfile: "str | Path", keyfile: "str | Path | None" = None) -> CertInfo:
    """Controlla leggibilita', formato, coerenza certificato/chiave e validita' temporale. Ritorna le info del certificato.

    ``keyfile`` vuoto: la chiave si cerca nello stesso file del certificato (PEM combinato).
    """
    chain = _load_chain(certfile)
    leaf = chain[0]
    key = _load_key(keyfile or certfile)
    if _spki(leaf.public_key()) != _spki(key.public_key()):
        raise TlsError("Il certificato e la chiave privata non corrispondono (non sono la stessa coppia).")
    info = _info(leaf, str(certfile))
    now = dt.datetime.now(dt.timezone.utc)
    if info.not_after <= now:
        raise TlsError(f"Il certificato e' scaduto il {info.not_after.astimezone():%d/%m/%Y}: serve un certificato valido.")
    if info.not_before > now + dt.timedelta(minutes=5):
        raise TlsError(f"Il certificato non e' ancora valido (valido dal {info.not_before.astimezone():%d/%m/%Y %H:%M}).")
    try:                                                                            # ultimo controllo: lo stack TLS lo accetta?
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(str(certfile), str(keyfile) if keyfile else None)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise TlsError(f"Certificato o chiave non utilizzabili dal server: {getattr(exc, 'reason', None) or exc}") from None
    return info


# ------------------------------------------------------------------------------------------ generazione
def _local_names() -> tuple[list[str], list[str]]:
    """(nomi DNS, indirizzi IP) con cui i client possono raggiungere il PC."""
    names = ["localhost"]
    try:
        h = socket.gethostname()
        if h and h not in names:
            names.insert(0, h)
    except OSError:
        pass
    ips = ["127.0.0.1"]
    try:
        from server.gui.controller import local_addresses              # import tardivo: il controller importa questo modulo
        for ip in local_addresses():
            if ip not in ips:
                ips.append(ip)
    except Exception:                                                  # noqa: BLE001
        pass
    return names, ips


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def generate_self_signed(data_dir: "str | Path", key_type: str = "rsa", days: int = VALID_DAYS,
                         hostnames: Optional[list[str]] = None, addresses: Optional[list[str]] = None) -> CertInfo:
    """Crea (o sostituisce) ``<dati>/tls/server.crt`` e ``server.key``: autofirmato, SAN = PC, localhost, 127.0.0.1, IP locali.

    ``key_type``: ``rsa`` (RSA 2048, massima compatibilita') oppure ``ec`` (ECDSA P-256).
    I client che avevano accettato il vecchio certificato dovranno accettare di nuovo quello nuovo.
    """
    if key_type not in ("rsa", "ec"):
        raise ValueError("key_type deve essere 'rsa' o 'ec'")
    dns, ips = _local_names()
    dns = list(hostnames) if hostnames is not None else dns
    ips = list(addresses) if addresses is not None else ips
    key = rsa.generate_private_key(65537, 2048) if key_type == "rsa" else ec.generate_private_key(ec.SECP256R1())
    cn = dns[0] if dns else "Automation Project Server"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Automation Project Server")])
    alt: list[x509.GeneralName] = []
    for d in dns:
        try:
            alt.append(x509.DNSName(d))
        except ValueError:
            continue
    for ip in ips:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue
    now = dt.datetime.now(dt.timezone.utc)
    usage = x509.KeyUsage(digital_signature=True, key_encipherment=(key_type == "rsa"), content_commitment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=10)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(usage, critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = default_paths(data_dir)
    certfile.parent.mkdir(parents=True, exist_ok=True)
    _write_private(keyfile, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return _info(cert, str(certfile))


# ------------------------------------------------------------------------------------------ risoluzione all'avvio
def resolve_tls(data_dir: "str | Path", enabled: bool, certfile: str = "", keyfile: str = "") -> Optional[TlsMaterial]:
    """Prepara il materiale TLS per l'avvio del server.

    * ``enabled`` falso -> ``None`` (HTTP).
    * ``certfile`` indicato -> certificato dell'utente, validato (``TlsError`` se non coerente/scaduto/illeggibile).
    * altrimenti certificato autofirmato in ``<dati>/tls``: generato se manca, rigenerato se scaduto o illeggibile.
    """
    if not enabled:
        return None
    if certfile:
        info = validate_pair(certfile, keyfile or None)
        return TlsMaterial(str(certfile), str(keyfile or certfile), info)
    cert, key = default_paths(data_dir)
    generated = False
    if cert.exists() and key.exists():
        try:
            info = validate_pair(cert, key)
        except TlsError:
            info = generate_self_signed(data_dir)             # scaduto o danneggiato: se ne crea uno nuovo (i client dovranno riaccettarlo)
            generated = True
    else:
        info = generate_self_signed(data_dir)
        generated = True
    return TlsMaterial(str(cert), str(key), info, generated)


def scheme_for(enabled: bool) -> str:
    return "https" if enabled else "http"


def current_cert_info(data_dir: "str | Path", certfile: str = "") -> Optional[CertInfo]:
    """Info del certificato in uso (quello dell'utente o l'autofirmato) senza crearne di nuovi; ``None`` se manca/illeggibile."""
    try:
        return read_cert_info(certfile or default_paths(data_dir)[0])
    except TlsError:
        return None


def expiry_notice(info: CertInfo) -> Optional[str]:
    if info.days_left < EXPIRY_WARN_DAYS:
        return f"Il certificato TLS scade tra {max(info.days_left, 0)} giorni ({info.not_after.astimezone():%d/%m/%Y}): rinnovalo in tempo."
    return None
