"""Explicit synchronization workflow with offline cache fallback."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cache import PublicDataCache
from .client import (
    MAIN_MENU_URL,
    OFFICIAL_JSON_BASE,
    HttpFetcher,
    PublicDataError,
    UrllibHttpFetcher,
)
from .models import OfficialNotice, PublicDataFeed, PublicDataSyncResult
from .parser import (
    MenuArticle,
    notice_without_detail,
    parse_article_notice,
    parse_main_menu,
    select_recent_candidates,
)

logger = logging.getLogger(__name__)


class OfficialPublicDataService:
    """Fetch only when ``synchronize`` is called by an explicit UI action."""

    def __init__(
        self,
        cache_directory: str | Path,
        *,
        fetcher: HttpFetcher | None = None,
        now: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._cache = PublicDataCache(cache_directory)
        self._fetcher = fetcher or UrllibHttpFetcher()
        self._now = now

    def load_cached(self) -> PublicDataSyncResult:
        """Read local cache only; this method never creates network traffic."""

        feed = self._cache.load()
        if feed is None:
            logger.info("public_data_cache source=official_zh_tw status=missing")
            return PublicDataSyncResult(None, False, False, "尚未更新官方公開資料。")
        logger.info(
            "public_data_cache source=official_zh_tw status=loaded count=%d sha256=%s",
            len(feed.notices),
            feed.source_sha256[:12],
        )
        return PublicDataSyncResult(feed, True, False, "已載入上次的官方公開資料快取。")

    def synchronize(self) -> PublicDataSyncResult:
        """Perform a user-requested official GET and atomically replace known-good cache."""

        logger.info("public_data_sync source=official_zh_tw status=requested")
        cached = self._cache.load()
        conditional: dict[str, str] = {}
        if cached is not None and cached.schedule_schema_version == 1:
            if cached.etag:
                conditional["If-None-Match"] = cached.etag
            if cached.last_modified:
                conditional["If-Modified-Since"] = cached.last_modified
        try:
            menu_response = self._fetcher.get(MAIN_MENU_URL, headers=conditional)
            if menu_response.status == 304:
                if cached is None:
                    raise PublicDataError("官方資料回應未更新, 但本機沒有可用快取。")
                logger.info(
                    "public_data_sync source=official_zh_tw status=not_modified count=%d sha256=%s",
                    len(cached.notices),
                    cached.source_sha256[:12],
                )
                return PublicDataSyncResult(
                    cached,
                    True,
                    True,
                    "官方公開資料沒有變更, 繼續使用本機快取。",
                )
            if menu_response.status != 200:
                raise PublicDataError(f"官方資料伺服器回應 HTTP {menu_response.status}。")
            _require_json_content_type(menu_response.headers)
            menu_payload = _decode_json(menu_response.body)
            candidates = select_recent_candidates(parse_main_menu(menu_payload))
            with ThreadPoolExecutor(max_workers=6, thread_name_prefix="wuwa-official-get") as pool:
                loaded = tuple(pool.map(self._fetch_notice, candidates))
            previous = {item.article_id: item for item in cached.notices} if cached else {}
            notices = [
                notice if detail_ok else previous.get(notice.article_id, notice)
                for notice, detail_ok in loaded
            ]
            detail_failure_count = sum(not detail_ok for _notice, detail_ok in loaded)
            if not notices:
                raise PublicDataError("官方公告清單目前沒有可顯示的項目。")
            now = self._now()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("now must return a timezone-aware datetime")
            feed = PublicDataFeed(
                schema_version=1,
                source_url=MAIN_MENU_URL,
                fetched_at=now.astimezone(UTC).isoformat(timespec="seconds"),
                source_sha256=hashlib.sha256(menu_response.body).hexdigest(),
                last_modified=_header(menu_response.headers, "last-modified"),
                etag=_header(menu_response.headers, "etag"),
                notices=tuple(notices),
                schedule_schema_version=1,
            )
            self._cache.save(feed)
            logger.info(
                "public_data_sync source=official_zh_tw status=updated count=%d "
                "detail_failures=%d sha256=%s",
                len(feed.notices),
                detail_failure_count,
                feed.source_sha256[:12],
            )
            return PublicDataSyncResult(
                feed,
                False,
                False,
                f"已從庫洛官方網站更新 {len(feed.notices)} 則公開公告。",
            )
        except (PublicDataError, OSError, ValueError) as exc:
            logger.warning(
                "public_data_sync source=official_zh_tw status=failed error_code=%s cache=%s",
                type(exc).__name__,
                cached is not None,
            )
            if cached is not None:
                return PublicDataSyncResult(
                    cached,
                    True,
                    False,
                    f"{exc} 已保留並顯示上次成功快取。",
                )
            raise PublicDataError(str(exc)) from exc

    def _fetch_notice(self, candidate: MenuArticle) -> tuple[OfficialNotice, bool]:
        article_url = f"{OFFICIAL_JSON_BASE}/article/{candidate.article_id}.json"
        try:
            response = self._fetcher.get(article_url)
            if response.status != 200:
                raise PublicDataError("官方文章回應非 200。")
            _require_json_content_type(response.headers)
            return parse_article_notice(candidate, _decode_json(response.body)), True
        except (PublicDataError, OSError, ValueError):
            return notice_without_detail(candidate), False


def _decode_json(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicDataError("官方資料不是有效的 UTF-8 JSON。") from exc


def _require_json_content_type(headers: Mapping[str, str]) -> None:
    value = _header(headers, "content-type")
    if value is None or value.split(";", 1)[0].strip().casefold() != "application/json":
        raise PublicDataError("官方資料回應不是 JSON。")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).casefold() == name.casefold() and isinstance(value, str):
            return value
    return None


__all__ = ["MAIN_MENU_URL", "OFFICIAL_JSON_BASE", "OfficialPublicDataService"]
