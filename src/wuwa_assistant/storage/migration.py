"""Programmatic Alembic runner for development and packaged desktop startup."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from .errors import MigrationError

if TYPE_CHECKING:
    from alembic.config import Config


def _alembic_config() -> Config:
    from alembic.config import Config

    config = Config()
    script_location = Path(__file__).resolve().parent / "migrations"
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("version_locations", str(script_location / "versions"))
    config.set_main_option("path_separator", "os")
    return config


def migrate_database(engine: Engine, database_path: Path | None = None) -> None:
    """Upgrade a database to the bundled schema head using the supplied connection."""

    from alembic import command
    from alembic.util.exc import CommandError

    path = database_path or Path(engine.url.database or "unknown.sqlite3")
    config = _alembic_config()
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except (CommandError, SQLAlchemyError, RuntimeError) as exc:
        raise MigrationError(path, str(exc)) from exc


__all__ = ["migrate_database"]
