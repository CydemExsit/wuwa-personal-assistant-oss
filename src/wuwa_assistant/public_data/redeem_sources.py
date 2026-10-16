"""Bounded, GET-only adapters for public code sources, with no account access."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .client import MAIN_MENU_URL, OFFICIAL_JSON_BASE, PublicDataError, UrllibHttpFetcher
from .parser import parse_main_menu
from .redeem_models import CODE_PATTERN, COMMUNITY_URL, RedeemCode


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored += 1
        if tag in {"p", "div", "li", "br", "h2", "h3", "tr"} and not self._ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._ignored = max(0, self._ignored - 1)
        if tag in {"p", "div", "li", "h2", "h3", "tr"} and not self._ignored:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


def _text(html: str) -> str:
    if len(html) > 2_000_000:
        raise ValueError("code source too large")
    parser = _Text()
    parser.feed(html)
    return "".join(parser.parts)


def parse_community(html: str) -> tuple[RedeemCode, ...]:
    text = _text(html)
    # Fail closed when editorial structure changes; never scan unrelated prose.
    active = re.search(
        r"(?:Here are all the active (?:WuWa|Wuthering Waves) codes:)(.*?)(?:How do I redeem)",
        text,
        re.S | re.I,
    )
    expired = re.search(r"Expired codes:\s*([^\n]+)", text, re.I)
    if active is None or expired is None:
        raise ValueError("公開彙整站格式改變, 已保留快取。")
    found: dict[str, RedeemCode] = {}
    for match in re.finditer(r"(?:^|\n)\s*([A-Z0-9]{6,40})\s*[-\u2013\u2014]", active[1]):
        code = RedeemCode(match[1], COMMUNITY_URL, "listed")
        found[code.key] = code
    if not found:
        raise ValueError("公開彙整站未提供可確認的可用碼清單。")
    for token in expired[1].split("|"):
        value = token.strip()
        if CODE_PATTERN.fullmatch(value) and any(c.isalpha() for c in value):
            code = RedeemCode(value, COMMUNITY_URL, "expired")
            found.setdefault(code.key, code)
    return tuple(found.values())


def parse_official(html: str, article_id: int) -> tuple[RedeemCode, ...]:
    from .client import official_article_url

    text = _text(html)
    # Only a labelled value is accepted, never arbitrary uppercase article text,
    # image URLs, referral links, dates or an article's generic event deadline.
    matches = re.finditer(
        r"(?:兌換碼|兑换码|序號|序号|禮包碼|礼包码|redemption code)"
        r"\s*[\uFF1A:]\s*([A-Za-z0-9]{6,40})(?![A-Za-z0-9])",
        text,
        re.I,
    )
    found: dict[str, RedeemCode] = {}
    for match in matches:
        if any(c.isalpha() for c in match[1]):
            code = RedeemCode(match[1], official_article_url(article_id))
            found[code.key] = code
    return tuple(found.values())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        raise PublicDataError("兌換碼來源重新導向已被拒絕。")


def fetch_community() -> tuple[RedeemCode, ...]:
    request = Request(
        COMMUNITY_URL,
        headers={"User-Agent": "WuwaPersonalAssistant/1.0 (public-data; no account data)"},
        method="GET",
    )
    with build_opener(_NoRedirect()).open(request, timeout=8) as response:
        if response.status != 200 or response.geturl() != COMMUNITY_URL:
            raise PublicDataError("兌換碼來源回應異常。")
        if response.headers.get_content_type() != "text/html":
            raise PublicDataError("兌換碼來源不是 HTML。")
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise PublicDataError("兌換碼來源超過大小限制。")
        return parse_community(body.decode("utf-8"))


def fetch_official() -> tuple[RedeemCode, ...]:
    fetcher = UrllibHttpFetcher()
    response = fetcher.get(MAIN_MENU_URL)
    if response.status != 200:
        raise PublicDataError("官方公告索引更新失敗。")
    articles = parse_main_menu(json.loads(response.body))
    candidates = sorted(
        (a for a in articles if re.search(r"兌換|序號|禮包|前瞻|特別節目|福利", a.title)),
        key=lambda a: (a.published_at or "", a.article_id),
        reverse=True,
    )[:8]

    def load(article_id: int) -> tuple[RedeemCode, ...]:
        response = fetcher.get(f"{OFFICIAL_JSON_BASE}/article/{article_id}.json")
        if response.status != 200:
            raise PublicDataError("官方兌換碼公告讀取失敗。")
        payload = json.loads(response.body)
        if not isinstance(payload, dict) or not isinstance(payload.get("articleContent"), str):
            raise ValueError("官方兌換碼公告格式改變。")
        return parse_official(payload["articleContent"], article_id)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="wuwa-redeem-official") as pool:
        return tuple(
            code for group in pool.map(load, (a.article_id for a in candidates)) for code in group
        )
