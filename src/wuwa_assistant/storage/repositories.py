"""Small repositories that keep SQLAlchemy records behind application boundaries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .orm import AppSettingRecord, utc_now


def _validated_json_value(value: Any) -> Any:
    """Reject non-JSON values and non-finite floats, then detach mutable inputs."""

    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("setting value must be finite JSON data") from exc
    return json.loads(rendered)


@dataclass(frozen=True, slots=True)
class StoredSetting:
    key: str
    value: Any
    value_schema_version: int
    updated_at: datetime


class SettingsRepository:
    """Transaction-scoped key/value settings access."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, key: str) -> StoredSetting | None:
        record = self._session.get(AppSettingRecord, key)
        return self._to_stored(record) if record is not None else None

    def get_value(self, key: str, default: Any = None) -> Any:
        setting = self.get(key)
        return deepcopy(default if setting is None else setting.value)

    def list_all(self) -> list[StoredSetting]:
        records = self._session.scalars(
            select(AppSettingRecord).order_by(AppSettingRecord.key)
        ).all()
        return [self._to_stored(record) for record in records]

    def set(self, key: str, value: Any, *, value_schema_version: int = 1) -> StoredSetting:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("setting key must not be empty")
        if len(normalized_key) > 200:
            raise ValueError("setting key must not exceed 200 characters")
        if value_schema_version < 1:
            raise ValueError("value_schema_version must be positive")

        safe_value = _validated_json_value(value)
        now = utc_now()
        record = self._session.get(AppSettingRecord, normalized_key)
        if record is None:
            record = AppSettingRecord(
                key=normalized_key,
                value_json=safe_value,
                value_schema_version=value_schema_version,
                updated_at=now,
            )
            self._session.add(record)
        else:
            record.value_json = safe_value
            record.value_schema_version = value_schema_version
            record.updated_at = now
        self._session.flush()
        return self._to_stored(record)

    def delete(self, key: str) -> bool:
        record = self._session.get(AppSettingRecord, key)
        if record is None:
            return False
        self._session.delete(record)
        self._session.flush()
        return True

    @staticmethod
    def _to_stored(record: AppSettingRecord) -> StoredSetting:
        return StoredSetting(
            key=record.key,
            value=deepcopy(record.value_json),
            value_schema_version=record.value_schema_version,
            updated_at=record.updated_at,
        )


__all__ = ["SettingsRepository", "StoredSetting"]
