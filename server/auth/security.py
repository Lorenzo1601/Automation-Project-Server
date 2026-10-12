"""Hashing password (bcrypt) e token JWT (access + refresh con rotazione)."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import bcrypt
import jwt

ISSUER = "automation-project-server"
_BCRYPT_ROUNDS = int(os.getenv("APS_BCRYPT_ROUNDS", "12"))


class TokenError(Exception):
    pass


# --------------------------------------------------------------------------- password
def _prehash(password: str) -> bytes:
    # bcrypt tronca a 72 byte: si pre-hasha con SHA-256 (base64) per supportare password lunghe
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str, rounds: Optional[int] = None) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds or _BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode("ascii"))
    except ValueError:
        return False


def validate_password_policy(password: str) -> None:
    """Minimo 10 caratteri, almeno una lettera e una cifra."""
    if len(password) < 10 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("La password deve avere almeno 10 caratteri, con almeno una lettera e una cifra")


def generate_password() -> str:
    return secrets.token_urlsafe(9) + "a1"


# --------------------------------------------------------------------------- segreto JWT
def load_or_create_secret(data_dir: Path, configured: Optional[str]) -> str:
    if configured:
        if len(configured) < 32:
            raise ValueError("APS_JWT_SECRET deve avere almeno 32 caratteri")
        return configured
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "jwt_secret"
    if p.exists():
        return p.read_text("utf-8").strip()
    sec = secrets.token_urlsafe(48)
    p.write_text(sec, "utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return sec


# --------------------------------------------------------------------------- JWT
class TokenService:
    def __init__(self, secret: str, access_ttl: timedelta, refresh_ttl: timedelta):
        self.secret, self.access_ttl, self.refresh_ttl = secret, access_ttl, refresh_ttl

    def _encode(self, typ: str, sub: int, sid: str, ttl: timedelta, extra: Optional[dict] = None) -> str:
        now = datetime.now(timezone.utc)
        claims: dict[str, Any] = {"iss": ISSUER, "typ": typ, "sub": str(sub), "sid": sid,
                                  "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex}
        claims.update(extra or {})
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def access(self, user_id: int, sid: str, username: str, role: str) -> str:
        return self._encode("access", user_id, sid, self.access_ttl, {"usr": username, "role": role})

    def refresh(self, user_id: int, sid: str, jti: str) -> str:
        tok = self._encode("refresh", user_id, sid, self.refresh_ttl)
        claims = jwt.decode(tok, options={"verify_signature": False})
        claims["jti"] = jti
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def decode(self, token: str, expected: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], issuer=ISSUER,
                                options={"require": ["exp", "iat", "sub", "sid", "jti", "typ"]})
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("Token scaduto") from exc
        except jwt.PyJWTError as exc:
            raise TokenError("Token non valido") from exc
        if claims.get("typ") != expected:
            raise TokenError("Tipo di token errato")
        return claims


# --------------------------------------------------------------------------- anti brute-force
class LoginThrottle:
    """Blocco temporaneo dopo N tentativi falliti consecutivi per lo stesso utente."""

    def __init__(self, max_failures: int = 5, lockout_seconds: float = 300.0):
        self.max, self.lock_s = max_failures, lockout_seconds
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def remaining(self, key: str) -> float:
        with self._lock:
            n, until = self._state.get(key, (0, 0.0))
            return max(0.0, until - time.monotonic()) if n >= self.max else 0.0

    def fail(self, key: str) -> None:
        with self._lock:
            n, until = self._state.get(key, (0, 0.0))
            if n >= self.max and until <= time.monotonic():
                n = 0                                   # il blocco e' scaduto: riparte il conteggio
            n += 1
            self._state[key] = (n, time.monotonic() + self.lock_s if n >= self.max else 0.0)

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)
