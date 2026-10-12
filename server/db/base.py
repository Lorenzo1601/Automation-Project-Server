"""Engine e sessioni SQLAlchemy (SQLite di default, PostgreSQL via APS_DATABASE_URL)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        self.url = url
        kw: dict = {"future": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _):          # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self._maker = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        from server.db import models  # noqa: F401  (registra le tabelle)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self._maker()

    @contextmanager
    def scope(self) -> Iterator[Session]:
        s = self._maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        self.engine.dispose()
