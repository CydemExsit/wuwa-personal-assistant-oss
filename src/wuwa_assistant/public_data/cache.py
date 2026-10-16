"""Atomic known-good cache for public official notice data."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .client import MAIN_MENU_URL, PublicDataError, validate_official_article_url
from .models import OfficialNotice, OfficialSchedule, PublicDataFeed

_CACHE_FILENAME = "official-feed.json"
_CATEGORIES = frozenset({"announcement", "event", "gacha"})
_DATE_STATUSES = frozenset({"needs_manual_confirmation", "confirmed_from_article_body"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PublicDataCache:
    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def path(self) -> Path:
        return self._directory / _CACHE_FILENAME

    def load(self) -> PublicDataFeed | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _feed_from_json(payload)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, feed: PublicDataFeed) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        payload = _feed_to_json(feed)
        _feed_from_json(payload)
        # Foreground refresh and the scheduled notifier may update together.
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="official-feed-", suffix=".json.tmp", dir=self._directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _feed_to_json(feed: PublicDataFeed) -> dict[str, object]:
    return {
        "schema_version": feed.schema_version,
        "schedule_schema_version": feed.schedule_schema_version,
        "source_url": feed.source_url,
        "fetched_at": feed.fetched_at,
        "source_sha256": feed.source_sha256,
        "last_modified": feed.last_modified,
        "etag": feed.etag,
        "notices": [
            {
                "article_id": item.article_id,
                "title": item.title,
                "category": item.category,
                "official_url": item.official_url,
                "published_at": item.published_at,
                "starts_on": item.starts_on,
                "ends_on": item.ends_on,
                "date_status": item.date_status,
                "schedules": [_schedule_to_json(schedule) for schedule in item.schedules],
            }
            for item in feed.notices
        ],
    }


def _feed_from_json(payload: Any) -> PublicDataFeed:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported cache schema")
    if payload.get("schedule_schema_version", 0) not in {0, 1}:
        raise ValueError("unsupported schedule schema")
    raw_notices = payload["notices"]
    if not isinstance(raw_notices, list) or len(raw_notices) > 100:
        raise ValueError("invalid notice cache")
    notices: list[OfficialNotice] = []
    for raw in raw_notices:
        if not isinstance(raw, dict):
            raise ValueError("invalid notice")
        article_id = raw["article_id"]
        title = raw["title"]
        category = raw["category"]
        official_url = raw["official_url"]
        date_status = raw["date_status"]
        if (
            type(article_id) is not int
            or article_id <= 0
            or article_id > 9_999_999_999
            or not all(
                isinstance(value, str) for value in (title, category, official_url, date_status)
            )
            or not title
            or len(title) > 300
            or category not in _CATEGORIES
            or date_status not in _DATE_STATUSES
        ):
            raise ValueError("invalid notice fields")
        try:
            validate_official_article_url(official_url, article_id=article_id)
        except PublicDataError as exc:
            raise ValueError("invalid official URL") from exc
        published_at = _optional_datetime(raw.get("published_at"))
        starts_on = _optional_date(raw.get("starts_on"))
        ends_on = _optional_date(raw.get("ends_on"))
        if (starts_on is None) != (ends_on is None):
            raise ValueError("incomplete activity date range")
        if starts_on is not None and ends_on is not None and ends_on < starts_on:
            raise ValueError("invalid activity date range")
        if date_status == "confirmed_from_article_body" and starts_on is None:
            raise ValueError("confirmed range missing")
        notices.append(
            OfficialNotice(
                article_id=article_id,
                title=title,
                category=category,
                official_url=official_url,
                published_at=published_at,
                starts_on=starts_on,
                ends_on=ends_on,
                date_status=date_status,
                schedules=_schedules_from_json(raw.get("schedules", []), article_id),
            )
        )
    source_url = _required_str(payload, "source_url")
    fetched_at = _required_str(payload, "fetched_at")
    source_sha256 = _required_str(payload, "source_sha256")
    if source_url != MAIN_MENU_URL:
        raise ValueError("invalid cache source")
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError as exc:
        raise ValueError("invalid fetched_at") from exc
    if fetched.tzinfo is None:
        raise ValueError("fetched_at must be timezone aware")
    if _SHA256.fullmatch(source_sha256) is None:
        raise ValueError("invalid source hash")
    return PublicDataFeed(
        schema_version=1,
        source_url=source_url,
        fetched_at=fetched_at,
        source_sha256=source_sha256,
        last_modified=_optional_str(payload.get("last_modified")),
        etag=_optional_str(payload.get("etag")),
        notices=tuple(notices),
        schedule_schema_version=payload.get("schedule_schema_version", 0),
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {key}")
    return value


def _schedule_to_json(item: OfficialSchedule) -> dict[str, object]:
    return {
        "key": item.key,
        "title": item.title,
        "kind": item.kind,
        "official_url": item.official_url,
        "starts_at": item.starts_at.isoformat() if item.starts_at else None,
        "ends_at": item.ends_at.isoformat() if item.ends_at else None,
        "five_star_characters": list(item.five_star_characters),
        "four_star_characters": list(item.four_star_characters),
        "start_label": item.start_label,
        "end_label": item.end_label,
        "version": item.version,
        "bonus_kind": item.bonus_kind,
        "daily_bonus_limit": item.daily_bonus_limit,
        "bonus_scopes": list(item.bonus_scopes),
    }


def _schedules_from_json(value: object, article_id: int) -> tuple[OfficialSchedule, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("invalid schedules")
    result: list[OfficialSchedule] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("invalid schedule")
        strings = {
            key: _required_str(raw, key)
            for key in ("key", "title", "kind", "official_url", "start_label", "end_label")
        }
        if any(len(item) > 1000 for item in strings.values()):
            raise ValueError("oversized schedule text")
        try:
            validate_official_article_url(strings["official_url"], article_id=article_id)
        except PublicDataError as exc:
            raise ValueError("invalid schedule URL") from exc
        names: dict[str, tuple[str, ...]] = {}
        for key in ("five_star_characters", "four_star_characters"):
            items = raw.get(key, [])
            if (
                not isinstance(items, list)
                or len(items) > 30
                or any(not isinstance(name, str) or not name or len(name) > 100 for name in items)
            ):
                raise ValueError("invalid schedule characters")
            names[key] = tuple(items)
        starts = _optional_datetime(raw.get("starts_at"))
        ends = _optional_datetime(raw.get("ends_at"))
        scopes = raw.get("bonus_scopes", [])
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise ValueError("invalid bonus scopes")
        result.append(
            OfficialSchedule(
                key=strings["key"],
                title=strings["title"],
                kind=strings["kind"],
                official_url=strings["official_url"],
                starts_at=datetime.fromisoformat(starts) if starts else None,
                ends_at=datetime.fromisoformat(ends) if ends else None,
                five_star_characters=names["five_star_characters"],
                four_star_characters=names["four_star_characters"],
                start_label=strings["start_label"],
                end_label=strings["end_label"],
                version=_optional_str(raw.get("version")),
                bonus_kind=_optional_str(raw.get("bonus_kind")),
                daily_bonus_limit=raw.get("daily_bonus_limit"),
                bonus_scopes=tuple(scopes),
            )
        )
    return tuple(result)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid optional string")
    return value


def _optional_datetime(value: object) -> str | None:
    result = _optional_str(value)
    if result is None:
        return None
    try:
        parsed = datetime.fromisoformat(result)
    except ValueError as exc:
        raise ValueError("invalid cached datetime") from exc
    if parsed.year >= 2090:
        raise ValueError("sentinel datetime is not a real date")
    return result


def _optional_date(value: object) -> str | None:
    result = _optional_str(value)
    if result is None:
        return None
    try:
        parsed = date.fromisoformat(result)
    except ValueError as exc:
        raise ValueError("invalid cached date") from exc
    if parsed.year >= 2090:
        raise ValueError("sentinel date is not a real date")
    return result


__all__ = ["PublicDataCache"]
