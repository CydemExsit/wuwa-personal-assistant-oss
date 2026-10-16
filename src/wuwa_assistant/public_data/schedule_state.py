"""Dataset-scoped completion flags shared by the UI and background reminders."""

from __future__ import annotations

import hashlib
from datetime import datetime

from wuwa_assistant.storage.settings import SettingsService

from .schedule import weekly_key


class ScheduleCompletionService:
    def __init__(self, settings: SettingsService) -> None:
        self._settings = settings

    @staticmethod
    def _prefix(data_set_id: str) -> str:
        if not data_set_id.strip():
            raise ValueError("data_set_id must not be empty")
        digest = hashlib.sha256(data_set_id.encode("utf-8")).hexdigest()
        return f"schedule.completed.{digest}."

    def completed_keys(self, *, data_set_id: str, now: datetime | None = None) -> frozenset[str]:
        del (
            now
        )  # Weekly keys include their exact period; old completions cannot suppress a new week.
        prefix = self._prefix(data_set_id)
        return frozenset(
            value["key"]
            for setting in self._settings.list_all()
            if setting.key.startswith(prefix)
            and isinstance(value := setting.value, dict)
            and value.get("completed") is True
            and isinstance(value.get("key"), str)
        )

    def set_completed(self, *, data_set_id: str, key: str, completed: bool) -> None:
        if not key or len(key) > 1000:
            raise ValueError("invalid completion key")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        self._settings.set(
            self._prefix(data_set_id) + digest,
            {"key": key, "completed": completed},
        )

    @staticmethod
    def weekly_key(now: datetime) -> str:
        return weekly_key(now)
