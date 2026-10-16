"""Small settings abstraction used by the Qt user interface.

The UI deliberately depends on this protocol instead of the database layer.  The
application bootstrap may later provide a database-backed service, while the initial
desktop shell can wrap a local :class:`QSettings` instance directly.
"""

from __future__ import annotations

from typing import Protocol

from PySide6.QtCore import QSettings


class SettingsStore(Protocol):
    """Minimal key/value contract required by the desktop shell."""

    def get(self, key: str, default: object | None = None) -> object | None:
        """Return a stored value or ``default`` when the key is absent."""

    def set(self, key: str, value: object) -> None:
        """Persist one setting value."""

    def sync(self) -> None:
        """Flush pending changes, if the backend buffers them."""


class QSettingsStore:
    """Adapt Qt's settings API to :class:`SettingsStore`."""

    def __init__(self, settings: QSettings) -> None:
        self._settings = settings

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._settings.value(key, default)

    def set(self, key: str, value: object) -> None:
        self._settings.setValue(key, value)

    def sync(self) -> None:
        self._settings.sync()


class NullSettingsStore:
    """No-op fallback that keeps the UI independently constructible."""

    def get(self, key: str, default: object | None = None) -> object | None:
        del key
        return default

    def set(self, key: str, value: object) -> None:
        del key, value

    def sync(self) -> None:
        return None


def coerce_settings_store(
    settings: SettingsStore | QSettings | None,
) -> SettingsStore:
    """Accept either the UI protocol or a raw ``QSettings`` instance."""

    if settings is None:
        return NullSettingsStore()
    if isinstance(settings, QSettings):
        return QSettingsStore(settings)
    return settings


def setting_bool(value: object | None, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def setting_int(value: object | None, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return default


def setting_float(value: object | None, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return default


def setting_str(value: object | None, default: str) -> str:
    return value if isinstance(value, str) else default
