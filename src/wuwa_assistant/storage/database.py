"""High-level construction of a ready, migrated local database."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from .engine import SessionFactory, create_session_factory, create_sqlite_engine
from .health import DatabaseHealth, inspect_database_health
from .migration import migrate_database
from .migrations import BUNDLED_SCHEMA_REVISION
from .paths import AppPaths
from .settings import SettingsService
from .unit_of_work import SqlAlchemyUnitOfWork


@dataclass(slots=True)
class Database:
    paths: AppPaths
    engine: Engine
    session_factory: SessionFactory
    health: DatabaseHealth

    @property
    def settings(self) -> SettingsService:
        return SettingsService(self.session_factory)

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.session_factory)

    def dispose(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.dispose()


def create_database(paths: AppPaths | None = None, *, echo_sql: bool = False) -> Database:
    """Create directories, check existing bytes, migrate, and verify the local DB."""

    resolved_paths = paths or AppPaths.default()
    resolved_paths.ensure_created()
    database_path = resolved_paths.database_path
    existed_with_content = database_path.is_file() and database_path.stat().st_size > 0
    engine = create_sqlite_engine(database_path, echo=echo_sql)

    try:
        health = None
        if existed_with_content:
            health = inspect_database_health(engine, database_path, require_schema=False)
        if health is None or health.schema_revision != BUNDLED_SCHEMA_REVISION:
            migrate_database(engine, database_path)
            health = inspect_database_health(engine, database_path, require_schema=True)
    except Exception:
        engine.dispose()
        raise

    return Database(
        paths=resolved_paths,
        engine=engine,
        session_factory=create_session_factory(engine),
        health=health,
    )


__all__ = ["Database", "create_database"]
