from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("APS_DATA_DIR", "./data")))
    database_url: Optional[str] = field(default_factory=lambda: os.getenv("APS_DATABASE_URL"))   # default: SQLite in data_dir
    jwt_secret: Optional[str] = field(default_factory=lambda: os.getenv("APS_JWT_SECRET"))       # default: generato e salvato
    access_ttl_min: float = field(default_factory=lambda: float(_env("APS_ACCESS_TTL_MIN", "15")))
    refresh_ttl_h: float = field(default_factory=lambda: float(_env("APS_REFRESH_TTL_H", "12")))
    bcrypt_rounds: int = field(default_factory=lambda: int(_env("APS_BCRYPT_ROUNDS", "12")))
    admin_username: str = field(default_factory=lambda: _env("APS_ADMIN_USER", "admin"))
    admin_password: Optional[str] = field(default_factory=lambda: os.getenv("APS_ADMIN_PASSWORD"))
    max_login_failures: int = field(default_factory=lambda: int(_env("APS_MAX_LOGIN_FAILURES", "5")))
    lockout_seconds: float = field(default_factory=lambda: float(_env("APS_LOCKOUT_SECONDS", "300")))
    presence_ttl: float = field(default_factory=lambda: float(_env("APS_SESSION_TTL", "90")))
    max_upload_mb: int = field(default_factory=lambda: int(_env("APS_MAX_UPLOAD_MB", "200")))
    server_name: str = field(default_factory=lambda: _env("APS_SERVER_NAME", ""))        # nome mostrato ai client (default: PC)
    discovery_port: int = field(default_factory=lambda: int(_env("APS_DISCOVERY_PORT", "8751")))   # 0 = disattivata
    default_max_revisions: int = field(default_factory=lambda: int(_env("APS_DEFAULT_MAX_REVISIONS", "10")))   # revisioni conservate per progetto
    lock_ttl_h: float = field(default_factory=lambda: float(_env("APS_LOCK_TTL_H", "8")))    # durata della presa in carico (lock) del Master, in ore
    host: str = field(default_factory=lambda: _env("APS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("APS_PORT", "8750")))
    # TLS (HTTPS + WSS). Senza certfile si usa/crea il certificato autofirmato in <data>/tls (vedi server/tls.py)
    tls_enabled: bool = field(default_factory=lambda: _env("APS_TLS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"))
    tls_certfile: str = field(default_factory=lambda: _env("APS_TLS_CERTFILE", ""))
    tls_keyfile: str = field(default_factory=lambda: _env("APS_TLS_KEYFILE", ""))

    @property
    def scheme(self) -> str:
        return "https" if self.tls_enabled else "http"

    @property
    def display_name(self) -> str:
        import socket
        return self.server_name or socket.gethostname()

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'aps.db').resolve().as_posix()}"
