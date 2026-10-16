from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request

import pytest

from wuwa_assistant.public_data.cache import PublicDataCache
from wuwa_assistant.public_data.client import (
    HttpResult,
    PublicDataError,
    UrllibHttpFetcher,
    _AllowlistRedirectHandler,
)
from wuwa_assistant.public_data.models import OfficialNotice, PublicDataFeed
from wuwa_assistant.public_data.parser import (
    MenuArticle,
    parse_article_notice,
    parse_main_menu,
    select_recent_candidates,
)
from wuwa_assistant.public_data.service import MAIN_MENU_URL, OfficialPublicDataService


class _RouteFetcher:
    def __init__(self, routes: dict[str, list[HttpResult]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 8.0,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> HttpResult:
        del timeout_seconds, max_bytes
        request_headers = {} if headers is None else dict(headers)
        self.calls.append((url, request_headers))
        return self.routes[url].pop(0)


def _json_result(url: str, payload: object, **headers: str) -> HttpResult:
    return HttpResult(
        200,
        json.dumps(payload, ensure_ascii=False).encode(),
        {"Content-Type": "application/json", **headers},
        url,
    )


def _menu() -> dict[str, object]:
    return {
        "article": [
            {
                "articleId": 5226,
                "articleTitle": "[斟雨祝荷風]角色活動喚取",
                "articleType": 90,
                "startTime": "2026-07-29 11:00:00",
            },
            # The real contract may repeat an article with articleType=0.
            {
                "articleId": 5226,
                "articleTitle": "[斟雨祝荷風]角色活動喚取",
                "articleType": 0,
                "startTime": "2026-07-29 11:00:00",
            },
        ]
    }


def _article() -> dict[str, object]:
    return {
        "articleId": 5226,
        "articleTitle": "[斟雨祝荷風]角色活動喚取",
        "articleType": 90,
        "startTime": "2026-07-29 11:00:00",
        "endTime": "2099-01-01 00:00:00",
        "articleContent": (
            "<p>✦活動時間✦</p><p>2026年7月30日10:00 ~ 2026年8月19日11:59 (伺服器時間)</p>"
        ),
    }


def _cached_feed() -> PublicDataFeed:
    return PublicDataFeed(
        1,
        MAIN_MENU_URL,
        "2026-08-08T00:00:00+00:00",
        "a" * 64,
        "Fri, 07 Aug 2026 12:00:59 GMT",
        '"etag"',
        (
            OfficialNotice(
                1,
                "已知公告",
                "event",
                "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/1",
                None,
                None,
                None,
                "needs_manual_confirmation",
            ),
        ),
    )


def test_fixture_contract_deduplicates_menu_and_ignores_2099_deadline() -> None:
    menu = parse_main_menu(_menu())
    assert len(menu) == 1
    notice = parse_article_notice(menu[0], _article())
    assert notice.category == "gacha"
    assert notice.starts_on == "2026-07-30"
    assert notice.ends_on == "2026-08-19"
    assert "2099" not in (notice.ends_on or "")


def test_2099_without_one_explicit_body_range_stays_unknown() -> None:
    item = MenuArticle(7, "版本公告", 90, "2026-08-01T10:00:00")
    notice = parse_article_notice(
        item,
        {
            "articleId": 7,
            "articleTitle": "版本公告",
            "endTime": "2099-01-01 00:00:00",
            "articleContent": "<p>詳細日期請見遊戲內</p>",
        },
    )
    assert notice.ends_on is None
    assert notice.date_status == "needs_manual_confirmation"


def test_identical_range_repeated_for_character_and_weapon_is_still_confirmed() -> None:
    item = MenuArticle(5221, "角色/武器活動喚取", 90, "2026-07-29T11:10:00")
    repeated = (
        "<p>角色: 2026年7月30日10:00 ~ 2026年8月19日11:59</p>"
        "<p>武器: 2026年7月30日10:00 ~ 2026年8月19日11:59</p>"
    )
    notice = parse_article_notice(
        item,
        {"articleId": 5221, "articleTitle": item.title, "articleContent": repeated},
    )
    assert (notice.starts_on, notice.ends_on) == ("2026-07-30", "2026-08-19")


def test_candidate_selection_reserves_space_for_each_notice_category() -> None:
    newer_events = tuple(
        MenuArticle(
            1000 + index,
            f"版本活動 {index}",
            91,
            f"2026-08-{20 - index:02d}T12:00:00",
        )
        for index in range(12)
    )
    older_gacha = MenuArticle(5221, "角色/武器活動喚取", 90, "2026-07-29T11:10:00")
    announcements = tuple(
        MenuArticle(2000 + index, f"一般新聞 {index}", 89, f"2026-08-{10 - index:02d}T12:00:00")
        for index in range(3)
    )
    selected = select_recent_candidates(
        newer_events + announcements + (older_gacha,),
        maximum=9,
        minimum_per_category=3,
    )
    assert older_gacha in selected
    assert len(selected) <= 9


def test_successful_sync_is_atomic_known_good_and_304_reuses_cache(tmp_path: Path) -> None:
    article_url = MAIN_MENU_URL.rsplit("/", 1)[0] + "/article/5226.json"
    fetcher = _RouteFetcher(
        {
            MAIN_MENU_URL: [
                _json_result(
                    MAIN_MENU_URL,
                    _menu(),
                    ETag='"current"',
                    **{"Last-Modified": "Fri, 07 Aug 2026 12:00:59 GMT"},
                ),
                HttpResult(304, b"", {}, MAIN_MENU_URL),
            ],
            article_url: [_json_result(article_url, _article())],
        }
    )
    service = OfficialPublicDataService(
        tmp_path,
        fetcher=fetcher,
        now=lambda: datetime(2026, 8, 8, tzinfo=UTC),
    )

    first = service.synchronize()
    second = service.synchronize()

    assert first.feed is not None and first.feed.notices[0].ends_on == "2026-08-19"
    assert second.feed == first.feed
    assert second.not_modified and second.used_cache
    assert fetcher.calls[-1][1] == {
        "If-None-Match": '"current"',
        "If-Modified-Since": "Fri, 07 Aug 2026 12:00:59 GMT",
    }
    cached = json.loads((tmp_path / "official-feed.json").read_text(encoding="utf-8"))
    assert cached["source_url"] == MAIN_MENU_URL
    assert len(cached["source_sha256"]) == 64
    assert cached["last_modified"] == "Fri, 07 Aug 2026 12:00:59 GMT"


def test_invalid_json_keeps_last_known_good_cache(tmp_path: Path) -> None:
    PublicDataCache(tmp_path).save(_cached_feed())
    fetcher = _RouteFetcher(
        {
            MAIN_MENU_URL: [
                HttpResult(200, b"not-json", {"Content-Type": "application/json"}, MAIN_MENU_URL)
            ]
        }
    )
    result = OfficialPublicDataService(tmp_path, fetcher=fetcher).synchronize()
    assert result.used_cache
    assert result.feed == _cached_feed()
    assert "保留" in result.message


def test_invalid_schema_without_cache_fails_closed(tmp_path: Path) -> None:
    fetcher = _RouteFetcher({MAIN_MENU_URL: [_json_result(MAIN_MENU_URL, {"unexpected": []})]})
    with pytest.raises(PublicDataError, match="MainMenu"):
        OfficialPublicDataService(tmp_path, fetcher=fetcher).synchronize()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "account"),
        ("date_status", "guessed"),
        ("official_url", "https://example.com/phishing"),
    ],
)
def test_tampered_cache_cannot_create_arbitrary_clickable_notice(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    cache = PublicDataCache(tmp_path)
    cache.save(_cached_feed())
    payload = json.loads(cache.path.read_text(encoding="utf-8"))
    payload["notices"][0][field] = value
    cache.path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.load() is None


def test_tampered_cache_rejects_nonofficial_source(tmp_path: Path) -> None:
    cache = PublicDataCache(tmp_path)
    cache.save(_cached_feed())
    payload = json.loads(cache.path.read_text(encoding="utf-8"))
    payload["source_url"] = "https://example.com/MainMenu.json"
    cache.path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.load() is None


class _Response:
    status = 200

    def __init__(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        self._url = url
        self._body = body
        self.headers = headers

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int) -> bytes:
        return self._body[:amount]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> _Response:
        assert timeout == 8.0
        self.requests.append(request)
        return self.response


def test_urllib_fetcher_rejects_oversize_before_reading_body() -> None:
    opener = _Opener(
        _Response(
            MAIN_MENU_URL, b"{}", {"Content-Length": "101", "Content-Type": "application/json"}
        )
    )
    with pytest.raises(PublicDataError, match="大小"):
        UrllibHttpFetcher(opener=opener).get(MAIN_MENU_URL, max_bytes=100)  # type: ignore[arg-type]


def test_redirect_to_non_allowlisted_host_is_rejected() -> None:
    handler = _AllowlistRedirectHandler()
    with pytest.raises(PublicDataError, match="白名單"):
        handler.redirect_request(
            Request(MAIN_MENU_URL),
            None,
            302,
            "Found",
            {},
            "https://example.com/not-official.json",
        )


def test_http_request_has_no_user_or_account_payload() -> None:
    opener = _Opener(
        _Response(MAIN_MENU_URL, b"{}", {"Content-Length": "2", "Content-Type": "application/json"})
    )
    UrllibHttpFetcher(opener=opener).get(MAIN_MENU_URL)  # type: ignore[arg-type]
    request = opener.requests[0]
    headers = {key.casefold(): value for key, value in request.header_items()}
    assert request.data is None
    assert request.get_method() == "GET"
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert "referer" not in headers
    assert "account" not in request.full_url.casefold()
    assert "localappdata" not in request.full_url.casefold()
