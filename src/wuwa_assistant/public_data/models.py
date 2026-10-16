"""Small immutable models for public, non-account website data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OfficialSchedule:
    """One explicitly named period from an official article, for the Asia server."""

    key: str
    title: str
    kind: str
    official_url: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    five_star_characters: tuple[str, ...] = ()
    four_star_characters: tuple[str, ...] = ()
    start_label: str = "開始時間待確認"
    end_label: str = "結束時間待確認"
    version: str | None = None
    bonus_kind: str | None = None
    daily_bonus_limit: int | None = None
    bonus_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"gacha", "event"}:
            raise ValueError("invalid schedule kind")
        if self.bonus_kind not in {None, "tacet", "forgery", "simulation", "mixed"}:
            raise ValueError("invalid bonus kind")
        if any(scope not in {"tacet", "forgery", "simulation"} for scope in self.bonus_scopes):
            raise ValueError("invalid bonus scopes")
        if self.daily_bonus_limit is not None and (
            type(self.daily_bonus_limit) is not int or not 1 <= self.daily_bonus_limit <= 100
        ):
            raise ValueError("invalid daily bonus limit")
        for value in (self.starts_at, self.ends_at):
            if value is not None and (value.tzinfo is None or value.year >= 2090):
                raise ValueError("schedule timestamps must be aware and real")
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValueError("schedule ends before it starts")


@dataclass(frozen=True, slots=True)
class OfficialNotice:
    """A conservatively parsed official website notice.

    ``starts_on`` and ``ends_on`` are populated only when an explicit date range
    is present in the article body.  The CMS publication timestamp is kept
    separately and is never presented as an activity deadline.
    """

    article_id: int
    title: str
    category: str
    official_url: str
    published_at: str | None
    starts_on: str | None
    ends_on: str | None
    date_status: str
    schedules: tuple[OfficialSchedule, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicDataFeed:
    schema_version: int
    source_url: str
    fetched_at: str
    source_sha256: str
    last_modified: str | None
    etag: str | None
    notices: tuple[OfficialNotice, ...]
    schedule_schema_version: int = 0


@dataclass(frozen=True, slots=True)
class PublicDataSyncResult:
    feed: PublicDataFeed | None
    used_cache: bool
    not_modified: bool
    message: str
