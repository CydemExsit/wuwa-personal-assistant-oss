"""User-marked farming needs scoped to a dataset and character slot."""

from threading import Lock

from wuwa_assistant.storage.settings import SettingsService


class EchoDemandService:
    def __init__(self, settings: SettingsService, data_set_id: str) -> None:
        self._settings = settings
        self._key = f"planning/echo_demand.v1/{data_set_id}"
        self._lock = Lock()

    def list(self) -> dict[tuple[str, int], int]:
        raw = self._settings.get(self._key, {})
        if not isinstance(raw, dict):
            return {}
        result: dict[tuple[str, int], int] = {}
        for character_id, slots in raw.items():
            if isinstance(character_id, str) and isinstance(slots, dict):
                for slot, cost in slots.items():
                    if (
                        slot in {"1", "2", "3", "4", "5"}
                        and type(cost) is int
                        and cost in (1, 3, 4)
                    ):
                        result[character_id, int(slot)] = cost
        return result

    def set(self, character_id: str, slot: int, cost: int | None) -> None:
        if not character_id or type(slot) is not int or not 1 <= slot <= 5:
            raise ValueError("請選擇角色的聲骸位置。")
        if cost is not None and (type(cost) is not int or cost not in (1, 3, 4)):
            raise ValueError("請確認需求的 COST。")
        with self._lock:
            rows = self.list()
            if cost is None:
                rows.pop((character_id, slot), None)
            else:
                rows[character_id, slot] = cost
            data: dict[str, dict[str, int]] = {}
            for (owner, position), value in rows.items():
                data.setdefault(owner, {})[str(position)] = value
            self._settings.set(self._key, data)
