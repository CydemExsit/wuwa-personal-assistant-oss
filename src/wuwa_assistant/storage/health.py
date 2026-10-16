"""SQLite integrity and configuration checks with user-presentable failures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, inspect
from sqlalchemy.exc import DatabaseError, SQLAlchemyError

from .errors import DatabaseCorruptionError, DatabaseHealthError


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    database_path: Path
    quick_check: str
    foreign_keys_enabled: bool
    journal_mode: str
    schema_revision: str | None

    @property
    def is_healthy(self) -> bool:
        return self.quick_check == "ok" and self.foreign_keys_enabled


def inspect_database_health(
    engine: Engine,
    database_path: Path,
    *,
    require_schema: bool = True,
) -> DatabaseHealth:
    """Inspect one database without repairing, replacing, or deleting it."""

    try:
        with engine.connect() as connection:
            quick_rows = tuple(map(str, connection.exec_driver_sql("PRAGMA quick_check").scalars()))
            quick_check = "; ".join(quick_rows) if quick_rows else "no result"
            foreign_keys_enabled = bool(
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            )
            journal_mode = str(
                connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            ).casefold()

            table_names = set(inspect(connection).get_table_names())
            schema_revision: str | None = None
            if "schema_version" in table_names:
                schema_revision_value = connection.exec_driver_sql(
                    "SELECT version_num FROM schema_version"
                ).scalar_one_or_none()
                if schema_revision_value is not None:
                    schema_revision = str(schema_revision_value)
    except DatabaseError as exc:
        raise DatabaseCorruptionError(database_path, str(exc.orig)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseHealthError(database_path, str(exc)) from exc

    if quick_check != "ok":
        raise DatabaseCorruptionError(database_path, quick_check)
    if not foreign_keys_enabled:
        raise DatabaseHealthError(database_path, "SQLite foreign key enforcement is disabled")
    if require_schema and schema_revision is None:
        raise DatabaseHealthError(database_path, "schema_version is missing or empty")

    return DatabaseHealth(
        database_path=database_path,
        quick_check=quick_check,
        foreign_keys_enabled=foreign_keys_enabled,
        journal_mode=journal_mode,
        schema_revision=schema_revision,
    )


__all__ = ["DatabaseHealth", "inspect_database_health"]
