"""Stable public entry points for local application persistence."""

from .database import Database, create_database
from .errors import (
    DatabaseCorruptionError,
    DatabaseHealthError,
    MigrationError,
    StorageError,
)
from .health import DatabaseHealth, inspect_database_health
from .migration import migrate_database
from .paths import AppPaths
from .repositories import SettingsRepository, StoredSetting
from .restore import RestoreActivationError, RestoreActivationResult, activate_staged_restore
from .settings import SettingsService
from .unit_of_work import SqlAlchemyUnitOfWork

__all__ = [
    "AppPaths",
    "Database",
    "DatabaseCorruptionError",
    "DatabaseHealth",
    "DatabaseHealthError",
    "MigrationError",
    "RestoreActivationError",
    "RestoreActivationResult",
    "SettingsRepository",
    "SettingsService",
    "SqlAlchemyUnitOfWork",
    "StorageError",
    "StoredSetting",
    "activate_staged_restore",
    "create_database",
    "inspect_database_health",
    "migrate_database",
]
