"""Versioned, offline character catalog used by the batch-add workflow.

The bundled catalog is intentionally conservative.  It contains no artwork,
descriptions, tier claims, account data, or runtime network client.  Unknown
facts remain null and the source status is kept on every entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from typing import Any

_KNOWN_ELEMENTS = frozenset({"氣動", "熱熔", "冷凝", "導電", "衍射", "湮滅"})
_KNOWN_WEAPON_TYPES = frozenset({"迅刀", "長刃", "佩槍", "臂鎧", "音感儀"})
_KNOWN_SOURCE_STATUSES = frozenset({"official_current_character", "public_name_index"})


@dataclass(frozen=True, slots=True)
class CatalogSource:
    title: str
    url: str
    kind: str
    status: str
    accessed_at: str


@dataclass(frozen=True, slots=True)
class CharacterCatalogEntry:
    identifier: str
    name: str
    element: str | None
    weapon_type: str | None
    rarity: int | None
    source_status: str


@dataclass(frozen=True, slots=True)
class CharacterCatalog:
    schema_version: int
    catalog_version: str
    as_of_date: str
    locale: str
    source_status: str
    disclaimer: str
    sources: tuple[CatalogSource, ...]
    entries: tuple[CharacterCatalogEntry, ...]

    def by_id(self) -> dict[str, CharacterCatalogEntry]:
        return {entry.identifier: entry for entry in self.entries}

    def search(self, query: str) -> tuple[CharacterCatalogEntry, ...]:
        needle = query.strip().casefold()
        if not needle:
            return self.entries
        return tuple(
            entry
            for entry in self.entries
            if needle
            in " ".join(
                part for part in (entry.name, entry.element, entry.weapon_type) if part is not None
            ).casefold()
        )


def load_bundled_character_catalog() -> CharacterCatalog:
    """Load and validate the packaged catalog without any network access."""

    resource = files("wuwa_assistant.data.characters").joinpath("catalog.v1.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    return parse_character_catalog(value)


def parse_character_catalog(value: object) -> CharacterCatalog:
    root = _object(value, "catalog")
    schema_version = _int(root.get("schema_version"), "schema_version")
    if schema_version != 1:
        raise ValueError("unsupported character catalog schema version")
    catalog_version = _text(root.get("catalog_version"), "catalog_version")
    as_of_date = _date_text(root.get("as_of_date"), "as_of_date")
    locale = _text(root.get("locale"), "locale")
    if locale != "zh-TW":
        raise ValueError("character catalog locale must be zh-TW")
    source_status = _text(root.get("source_status"), "source_status")
    disclaimer = _text(root.get("disclaimer"), "disclaimer")

    raw_sources = _list(root.get("sources"), "sources")
    sources = tuple(_parse_source(item) for item in raw_sources)
    if not sources or not any(source.kind == "official" for source in sources):
        raise ValueError("character catalog requires an official source")

    raw_entries = _list(root.get("entries"), "entries")
    entries = tuple(_parse_entry(item) for item in raw_entries)
    if not entries:
        raise ValueError("character catalog cannot be empty")
    identifiers = [entry.identifier for entry in entries]
    names = [entry.name.casefold() for entry in entries]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("character catalog identifiers must be unique")
    if len(names) != len(set(names)):
        raise ValueError("character catalog names must be unique")

    return CharacterCatalog(
        schema_version=schema_version,
        catalog_version=catalog_version,
        as_of_date=as_of_date,
        locale=locale,
        source_status=source_status,
        disclaimer=disclaimer,
        sources=sources,
        entries=entries,
    )


def _parse_source(value: object) -> CatalogSource:
    item = _object(value, "source")
    accessed_at = _date_text(item.get("accessed_at"), "source accessed_at")
    url = _text(item.get("url"), "source url")
    if not url.startswith("https://"):
        raise ValueError("character catalog source URL must use HTTPS")
    return CatalogSource(
        title=_text(item.get("title"), "source title"),
        url=url,
        kind=_text(item.get("kind"), "source kind"),
        status=_text(item.get("status"), "source status"),
        accessed_at=accessed_at,
    )


def _parse_entry(value: object) -> CharacterCatalogEntry:
    item = _object(value, "entry")
    element = _optional_text(item.get("element"), "element")
    if element is not None and element not in _KNOWN_ELEMENTS:
        raise ValueError(f"unknown character element: {element}")
    weapon_type = _optional_text(item.get("weapon_type"), "weapon_type")
    if weapon_type is not None and weapon_type not in _KNOWN_WEAPON_TYPES:
        raise ValueError(f"unknown character weapon type: {weapon_type}")
    rarity_value = item.get("rarity")
    rarity = None if rarity_value is None else _int(rarity_value, "rarity")
    if rarity is not None and rarity not in {4, 5}:
        raise ValueError("character rarity must be 4, 5, or null")
    source_status = _text(item.get("source_status"), "entry source_status")
    if source_status not in _KNOWN_SOURCE_STATUSES:
        raise ValueError(f"unknown character source status: {source_status}")
    return CharacterCatalogEntry(
        identifier=_text(item.get("id"), "entry id"),
        name=_text(item.get("name"), "entry name"),
        element=element,
        weapon_type=weapon_type,
        rarity=rarity,
        source_status=source_status,
    )


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be an object")
    return value


def _list(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _date_text(value: object, field_name: str) -> str:
    result = _text(value, field_name)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from exc
    return result
