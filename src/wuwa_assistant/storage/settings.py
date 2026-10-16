"""Application-facing persistent settings service."""

from __future__ import annotations

from typing import Any

from .engine import SessionFactory
from .repositories import StoredSetting
from .unit_of_work import SqlAlchemyUnitOfWork


class SettingsService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get(self, key: str, default: Any = None) -> Any:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            assert unit.settings is not None
            return unit.settings.get_value(key, default)

    def list_all(self) -> list[StoredSetting]:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            assert unit.settings is not None
            return unit.settings.list_all()

    def set(self, key: str, value: Any, *, value_schema_version: int = 1) -> StoredSetting:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            assert unit.settings is not None
            stored = unit.settings.set(
                key,
                value,
                value_schema_version=value_schema_version,
            )
            unit.commit()
            return stored

    def delete(self, key: str) -> bool:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            assert unit.settings is not None
            deleted = unit.settings.delete(key)
            unit.commit()
            return deleted


__all__ = ["SettingsService"]
