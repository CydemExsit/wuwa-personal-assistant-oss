"""Strict stdlib HTTP client for the official public JSON endpoints only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

OFFICIAL_CDN_HOST = "hw-media-cdn-mingchao.kurogame.com"
ALLOWED_FETCH_HOSTS = frozenset({OFFICIAL_CDN_HOST})
OFFICIAL_WEBSITE_HOST = "wutheringwaves.kurogames.com"
OFFICIAL_JSON_BASE = (
    "https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/zh-tw"
)
MAIN_MENU_URL = f"{OFFICIAL_JSON_BASE}/MainMenu.json"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class PublicDataError(RuntimeError):
    """Safe, user-displayable failure for public-data synchronization."""


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    body: bytes
    headers: Mapping[str, str]
    final_url: str


class HttpFetcher(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> HttpResult: ...


def validate_fetch_url(url: str) -> None:
    """Reject non-HTTPS, credential-bearing, or non-allowlisted URLs."""

    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise PublicDataError("官方資料連線只允許 HTTPS。")
    if parsed.username is not None or parsed.password is not None:
        raise PublicDataError("官方資料網址不得包含登入資訊。")
    if (
        parsed.port not in (None, 443)
        or (parsed.hostname or "").casefold() not in ALLOWED_FETCH_HOSTS
    ):
        raise PublicDataError("官方資料連線被主機白名單阻擋。")
    if parsed.fragment:
        raise PublicDataError("官方資料網址不得包含片段。")


def official_article_url(article_id: int) -> str:
    if type(article_id) is not int or article_id <= 0 or article_id > 9_999_999_999:
        raise ValueError("article_id must be a positive integer")
    return f"https://{OFFICIAL_WEBSITE_HOST}/zh-tw/main/news/detail/{article_id}"


def validate_official_article_url(url: str, *, article_id: int) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_WEBSITE_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or url != official_article_url(article_id)
    ):
        raise PublicDataError("公告連結不在庫洛官方網站白名單。")


class _AllowlistRedirectHandler(HTTPRedirectHandler):
    """Allow redirects only when their target remains on the exact allowlist."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        validate_fetch_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibHttpFetcher:
    """GET-only fetcher with bounded reads and no cookies or account headers."""

    def __init__(self, opener: OpenerDirector | None = None) -> None:
        self._opener = opener or build_opener(_AllowlistRedirectHandler())

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> HttpResult:
        validate_fetch_url(url)
        if timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError("timeout_seconds and max_bytes must be positive")
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "WuwaPersonalAssistant/1.0 (public-data; no account data)",
        }
        if headers:
            for key in ("If-None-Match", "If-Modified-Since"):
                value = headers.get(key)
                if value:
                    request_headers[key] = value
        request = Request(url, headers=request_headers, method="GET")
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except HTTPError as exc:
            if exc.code == 304:
                validate_fetch_url(exc.geturl())
                return HttpResult(304, b"", dict(exc.headers.items()), exc.geturl())
            raise PublicDataError(f"官方資料伺服器回應 HTTP {exc.code}。") from exc
        except (OSError, TimeoutError) as exc:
            raise PublicDataError("無法連線官方資料來源, 已保留本機快取。") from exc

        with response:
            final_url = response.geturl()
            validate_fetch_url(final_url)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        raise PublicDataError("官方資料回應超過安全大小限制。")
                except ValueError:
                    pass
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise PublicDataError("官方資料回應超過安全大小限制。")
            return HttpResult(
                int(response.status),
                body,
                dict(response.headers.items()),
                final_url,
            )


__all__ = [
    "ALLOWED_FETCH_HOSTS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAIN_MENU_URL",
    "OFFICIAL_JSON_BASE",
    "HttpFetcher",
    "HttpResult",
    "PublicDataError",
    "UrllibHttpFetcher",
    "official_article_url",
    "validate_fetch_url",
    "validate_official_article_url",
]
