"""Manual availability of the Echoes a player still wants from each field."""

from __future__ import annotations

from wuwa_assistant.storage.settings import SettingsService

from .tacet_catalog import TacetCatalog, TacetField

# Frozen initial preset for the reviewed 3.7 catalog. High Sky (13) and the
# three 3.7 sets (36/37/38) start exhausted. Future set IDs start exhausted too.
# This is a convenient editable preset, never an inferred account inventory.
_INITIAL_STOCKED_SETS = frozenset(
    {
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        14,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        33,
        34,
        35,
    }
)


def _signature(field: TacetField) -> list[str]:
    return sorted(field.set_names)


class TacetStockService:
    """Dataset-scoped settings; no counts, OCR guesses or automatic depletion."""

    def __init__(self, settings: SettingsService) -> None:
        self._settings = settings

    @staticmethod
    def _key(data_set_id: str) -> str:
        if not data_set_id.strip():
            raise ValueError("資料集識別碼不可為空。")
        return f"planning/tacet_stock.v1/{data_set_id}"

    def stocked_field_ids(self, data_set_id: str, catalog: TacetCatalog) -> frozenset[str]:
        raw = self._settings.get(self._key(data_set_id))
        saved = raw if isinstance(raw, dict) else {}
        stocked: set[str] = set()
        for field in catalog.fields:
            if field.field_id in saved:
                row = saved[field.field_id]
                # New drops in an existing field invalidate the old availability;
                # malformed settings must never conceal a farming requirement.
                has_stock = (
                    isinstance(row, dict)
                    and row.get("sets") == _signature(field)
                    and row.get("has_stock") is True
                )
            else:
                has_stock = bool(field.set_ids) and all(
                    identifier in _INITIAL_STOCKED_SETS for identifier in field.set_ids
                )
            if has_stock:
                stocked.add(field.field_id)
        return frozenset(stocked)

    def save(
        self,
        data_set_id: str,
        catalog: TacetCatalog,
        field_ids: tuple[str, ...],
        *,
        has_stock: bool,
    ) -> None:
        if type(has_stock) is not bool or not field_ids:
            raise ValueError("請選擇無音區及庫存狀態。")
        fields = {field.field_id: field for field in catalog.fields if field.verified}
        if any(identifier not in fields for identifier in field_ids):
            raise ValueError("無音區不存在或來源尚未確認。")
        key = self._key(data_set_id)
        raw = self._settings.get(key)
        saved = dict(raw) if isinstance(raw, dict) else {}
        for identifier in field_ids:
            saved[identifier] = {
                "sets": _signature(fields[identifier]),
                "has_stock": has_stock,
            }
        self._settings.set(key, saved)
