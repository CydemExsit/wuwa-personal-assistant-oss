from __future__ import annotations


class MemorySettings:
    """Small test double implementing the UI settings protocol."""

    def __init__(self, initial: dict[str, object] | None = None) -> None:
        self.values = dict(initial or {})
        self.sync_count = 0

    def get(self, key: str, default: object | None = None) -> object | None:
        return self.values.get(key, default)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value

    def sync(self) -> None:
        self.sync_count += 1
