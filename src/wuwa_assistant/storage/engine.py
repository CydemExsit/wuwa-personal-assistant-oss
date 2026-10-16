"""SQLite engine and session construction with safety-relevant PRAGMAs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

type SessionFactory = sessionmaker[Session]


def create_sqlite_engine(database_path: Path, *, echo: bool = False) -> Engine:
    """Create a file-backed SQLite engine suitable for short desktop transactions."""

    resolved = database_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(resolved)),
        echo=echo,
        pool_pre_ping=True,
        connect_args={"timeout": 5.0, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(
        dbapi_connection: sqlite3.Connection,
        _connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> SessionFactory:
    """Return a factory; callers must create one Session per worker/operation."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


__all__ = ["SessionFactory", "create_session_factory", "create_sqlite_engine"]
