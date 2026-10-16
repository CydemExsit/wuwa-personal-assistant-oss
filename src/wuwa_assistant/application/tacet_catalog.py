"""Validated offline Tacet Field facts; no network or account access on load."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

CATALOG_NAME = "tacet-fields.v1.json"
MAX_CATALOG_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class TacetField:
    field_id: str
    name: str
    region: str
    set_names: tuple[str, ...]
    source_url: str
    verified: bool
    available_from: str | None = None
    set_ids: tuple[int, ...] = ()
    map_id: int = 0
    mark_id: int = 0


@dataclass(frozen=True, slots=True)
class TacetCatalog:
    game_version: str
    retrieved_on: str
    fields: tuple[TacetField, ...]
    coverage_note: str
    aliases: tuple[tuple[str, str], ...]
    source_revision: str
    non_tacet_sets: tuple[str, ...] = ()


def _text(value: object, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("無音區資料包含無效文字。")
    return value.strip()


def parse_tacet_catalog(payload: object) -> TacetCatalog:
    """Reject partial, duplicate or untraceable imported catalogs as a whole."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("無音區資料版本不支援。")
    revision = _text(payload.get("source_revision"))
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("無音區資料缺少固定來源版本。")
    version = _text(payload.get("game_version"))
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("無音區遊戲版本無效。")
    retrieved_on = _text(payload.get("retrieved_on"))
    date.fromisoformat(retrieved_on)
    rows = payload.get("fields")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError("無音區清單無效。")
    if payload.get("source_field_count") != len(rows):
        raise ValueError("無音區清單不完整。")
    fields: list[TacetField] = []
    seen: set[str] = set()
    expected_url = (
        "https://github.com/Arikatsu/WutheringWaves_Data/blob/"
        f"{revision}/BinData/adventure_detect/silentareadetection.json"
    )
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("無音區資料格式無效。")
        identifier = _text(row.get("field_id"))
        if identifier in seen or not identifier.isdecimal():
            raise ValueError("無音區識別碼無效或重複。")
        seen.add(identifier)
        names = row.get("set_names")
        ids = row.get("set_ids")
        if (
            not isinstance(names, list)
            or len(names) != 2
            or len({_text(name) for name in names}) != 2
            or not isinstance(ids, list)
            or len(ids) != 2
            or any(type(item) is not int or item <= 0 for item in ids)
            or ids[0] == ids[1]
        ):
            raise ValueError("無音區必須有兩個已確認的不同套裝。")
        if row.get("source_url") != expected_url or row.get("verified") is not True:
            raise ValueError("無音區來源未通過核對。")
        for key in ("map_id", "mark_id"):
            if type(row.get(key)) is not int or row[key] <= 0:
                raise ValueError("無音區地圖識別碼無效。")
        available = row.get("available_from")
        if available is not None:
            date.fromisoformat(_text(available))
        fields.append(
            TacetField(
                identifier,
                _text(row.get("name")),
                _text(row.get("region")),
                tuple(names),
                expected_url,
                True,
                available,
                tuple(ids),
                row["map_id"],
                row["mark_id"],
            )
        )
    aliases = payload.get("aliases", {})
    if not isinstance(aliases, dict) or len(aliases) > 1000:
        raise ValueError("套裝別名資料無效。")
    canonical = {_text(name) for row in fields for name in row.set_names}
    non_tacet = payload.get("non_tacet_sets", [])
    if not isinstance(non_tacet, list) or len(non_tacet) > 500:
        raise ValueError("其他套裝資料無效。")
    canonical.update(_text(name) for name in non_tacet)
    normalized_aliases = tuple((_text(key), _text(value)) for key, value in aliases.items())
    if any(value not in canonical for _key, value in normalized_aliases):
        raise ValueError("套裝別名指向未知套裝。")
    return TacetCatalog(
        version,
        retrieved_on,
        tuple(fields),
        _text(payload.get("coverage_note"), maximum=2000),
        normalized_aliases,
        revision,
        tuple(non_tacet),
    )


def _read_catalog(path: Path) -> TacetCatalog:
    if path.stat().st_size > MAX_CATALOG_BYTES:
        raise ValueError("無音區快取超過大小限制。")
    return parse_tacet_catalog(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _bundled_catalog() -> TacetCatalog:
    resource = files("wuwa_assistant.data.planning").joinpath(CATALOG_NAME)
    return parse_tacet_catalog(json.loads(resource.read_text(encoding="utf-8")))


def load_tacet_catalog(cache_directory: str | Path | None = None) -> TacetCatalog:
    """Prefer a valid newer cache and retain bundled facts on any cache error."""
    bundled = _bundled_catalog()
    if cache_directory is not None:
        try:
            cached = _read_catalog(Path(cache_directory) / CATALOG_NAME)
            if _version(cached.game_version) >= _version(bundled.game_version):
                return cached
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return bundled


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def canonical_set_name(name: str) -> str:
    """Only documented exact aliases match; unknown names stay unknown."""
    value = name.strip()
    return dict(_bundled_catalog().aliases).get(value, value)


def bundled_catalog_payload() -> dict[str, Any]:
    """Return a fresh manifest for explicit maintenance and synchronization."""
    resource = files("wuwa_assistant.data.planning").joinpath(CATALOG_NAME)
    payload: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    return payload
