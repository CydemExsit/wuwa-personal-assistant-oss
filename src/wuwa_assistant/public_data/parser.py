"""Conservative schema and date parsing for the official CMS JSON."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from typing import Any

from .client import PublicDataError, official_article_url
from .models import OfficialNotice, OfficialSchedule
from .schedule import ASIA_ZONE

_MAX_MENU_ARTICLES = 5_000
_MAX_TITLE_LENGTH = 300
_MAX_HTML_LENGTH = 2_000_000
_GACHA_MARKERS = ("喚取",)
_EVENT_MARKERS = ("活動", "版本更新", "版本前瞻", "維護", "補償")
_DATE_RANGE = re.compile(
    r"(?P<sy>20\d{2})\s*年\s*(?P<sm>\d{1,2})\s*月\s*(?P<sd>\d{1,2})\s*日"
    r"(?:\s*(?P<sh>\d{1,2})\s*[:\uFF1A]\s*(?P<smin>\d{2}))?"
    r"\s*(?:~|\uFF5E|至|\u2014|\u2013)\s*"
    r"(?P<ey>20\d{2})\s*年\s*(?P<em>\d{1,2})\s*月\s*(?P<ed>\d{1,2})\s*日"
    r"(?:\s*(?P<eh>\d{1,2})\s*[:\uFF1A]\s*(?P<emin>\d{2}))?"
)


@dataclass(frozen=True, slots=True)
class MenuArticle:
    article_id: int
    title: str
    article_type: int | None
    published_at: str | None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"}:
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"}:
            self.parts.append("\n")


def parse_main_menu(payload: object) -> tuple[MenuArticle, ...]:
    """Validate only the fields used by the application and de-duplicate IDs."""

    if not isinstance(payload, dict):
        raise PublicDataError("官方 MainMenu JSON 格式不正確。")
    raw_articles = payload.get("article")
    if not isinstance(raw_articles, list) or len(raw_articles) > _MAX_MENU_ARTICLES:
        raise PublicDataError("官方 MainMenu JSON 缺少可用公告清單。")
    found: dict[int, MenuArticle] = {}
    for raw in raw_articles:
        if not isinstance(raw, dict):
            continue
        article_id = raw.get("articleId")
        title = raw.get("articleTitle")
        if type(article_id) is not int or article_id <= 0 or article_id > 9_999_999_999:
            continue
        if not isinstance(title, str):
            continue
        title = " ".join(title.split())
        if not title or len(title) > _MAX_TITLE_LENGTH:
            continue
        article_type_value = raw.get("articleType")
        article_type = article_type_value if type(article_type_value) is int else None
        published_at = _parse_cms_timestamp(raw.get("startTime"))
        candidate = MenuArticle(article_id, title, article_type, published_at)
        existing = found.get(article_id)
        # Prefer a real CMS category over the duplicate articleType=0 row.
        if existing is None or (existing.article_type == 0 and article_type not in (None, 0)):
            found[article_id] = candidate
    if not found:
        raise PublicDataError("官方 MainMenu JSON 沒有可確認的公告。")
    return tuple(found.values())


def select_recent_candidates(
    articles: tuple[MenuArticle, ...],
    *,
    maximum: int = 36,
    minimum_per_category: int = 8,
) -> tuple[MenuArticle, ...]:
    if maximum <= 0 or minimum_per_category <= 0 or maximum < minimum_per_category * 3:
        raise ValueError("candidate limits must be positive")
    relevant = [item for item in articles if classify_notice(item.title, item.article_type)]
    relevant.sort(key=lambda item: (item.published_at or "", item.article_id), reverse=True)
    selected_ids: set[int] = set()
    selected: list[MenuArticle] = []
    for category in ("gacha", "event", "announcement"):
        category_count = 0
        for item in relevant:
            if classify_notice(item.title, item.article_type) != category:
                continue
            selected.append(item)
            selected_ids.add(item.article_id)
            category_count += 1
            if category_count >= minimum_per_category:
                break
    for item in relevant:
        if len(selected) >= maximum:
            break
        if item.article_id not in selected_ids:
            selected.append(item)
            selected_ids.add(item.article_id)
    selected.sort(key=lambda item: (item.published_at or "", item.article_id), reverse=True)
    return tuple(selected[:maximum])


def classify_notice(title: str, article_type: int | None) -> str:
    if any(marker in title for marker in _GACHA_MARKERS):
        return "gacha"
    if article_type == 91 or any(marker in title for marker in _EVENT_MARKERS):
        return "event"
    return "announcement"


def parse_article_notice(menu: MenuArticle, payload: object) -> OfficialNotice:
    if not isinstance(payload, dict):
        raise PublicDataError("官方文章 JSON 格式不正確。")
    article_id = payload.get("articleId")
    if article_id != menu.article_id:
        raise PublicDataError("官方文章 JSON 識別碼不一致。")
    raw_title = payload.get("articleTitle")
    title = menu.title
    if isinstance(raw_title, str):
        clean_title = " ".join(raw_title.split())
        if clean_title and len(clean_title) <= _MAX_TITLE_LENGTH:
            title = clean_title
    content = payload.get("articleContent")
    starts_on: str | None = None
    ends_on: str | None = None
    date_status = "needs_manual_confirmation"
    if isinstance(content, str) and len(content) <= _MAX_HTML_LENGTH:
        starts_on, ends_on = _single_explicit_range(content)
        if starts_on is not None and ends_on is not None:
            date_status = "confirmed_from_article_body"
    # CMS uses 2099-01-01 as a sentinel.  We deliberately do not use endTime
    # (or publication startTime) as an activity period.
    return OfficialNotice(
        article_id=menu.article_id,
        title=title,
        category=classify_notice(title, menu.article_type),
        official_url=official_article_url(menu.article_id),
        published_at=_parse_cms_timestamp(payload.get("startTime")) or menu.published_at,
        starts_on=starts_on,
        ends_on=ends_on,
        date_status=date_status,
        schedules=(
            _article_schedules(menu.article_id, title, content)
            if isinstance(content, str) and len(content) <= _MAX_HTML_LENGTH
            else ()
        ),
    )


def notice_without_detail(menu: MenuArticle) -> OfficialNotice:
    """Keep a valid menu title when one optional detail request fails."""

    return OfficialNotice(
        article_id=menu.article_id,
        title=menu.title,
        category=classify_notice(menu.title, menu.article_type),
        official_url=official_article_url(menu.article_id),
        published_at=menu.published_at,
        starts_on=None,
        ends_on=None,
        date_status="needs_manual_confirmation",
    )


def _single_explicit_range(html: str) -> tuple[str | None, str | None]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, TypeError):
        return None, None
    text = " ".join(" ".join(parser.parts).split())
    ranges: set[tuple[str, str]] = set()
    for match in _DATE_RANGE.finditer(text):
        try:
            start = datetime(
                int(match["sy"]),
                int(match["sm"]),
                int(match["sd"]),
                int(match["sh"] or 0),
                int(match["smin"] or 0),
            )
            end = datetime(
                int(match["ey"]),
                int(match["em"]),
                int(match["ed"]),
                int(match["eh"] or 0),
                int(match["emin"] or 0),
            )
        except ValueError:
            continue
        if end >= start and end.year < 2090:
            ranges.add((start.date().isoformat(), end.date().isoformat()))
    if len(ranges) != 1:
        return None, None
    return next(iter(ranges))


def _parse_cms_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if parsed.year >= 2090:
        return None
    return parsed.isoformat(timespec="seconds")


_MOMENT = (
    r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"(?:\s*\d{1,2}\s*[:\uFF1A]\s*\d{2})?"
)
_PERIOD = re.compile(
    rf"(?P<start>{_MOMENT}|\d+\.\d+\s*版本更新後)\s*[~\uFF5E至—\u2013]\s*"
    rf"(?P<end>{_MOMENT})(?:\s*[\uFF08(](?P<zone>伺服器時間|UTC\s*[+\-\u2212]\s*\d{{1,2}}(?::\d{{2}})?)[\uFF09)])?",
    re.IGNORECASE,
)
_HEADING = re.compile(
    r"(?:^|(?<=。))[\[「【](?P<title>[^\]」】\n]{1,100})[\]」】](?P<type>[^\n]{0,30}(?:活動喚取|活動))\s*$",
    re.MULTILINE,
)


def _article_schedules(article_id: int, title: str, content: str) -> tuple[OfficialSchedule, ...]:
    parser = _TextExtractor()
    parser.feed(content)
    text = "\n".join(
        " ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip()
    )
    headings = tuple(_HEADING.finditer(text))
    schedules: list[OfficialSchedule] = []
    for index, heading in enumerate(headings):
        section = text[
            heading.end() : headings[index + 1].start() if index + 1 < len(headings) else None
        ]
        kind = heading["type"]
        if "武器" in kind:
            continue
        item = _schedule_from_section(
            article_id, heading["title"], "gacha" if "角色" in kind else "event", section
        )
        if item is not None:
            schedules.append(item)
    if not schedules and ("喚取" in title or "活動" in title) and "武器" not in title:
        item = _schedule_from_section(
            article_id, title, "gacha" if "喚取" in title else "event", text
        )
        if item is not None:
            schedules.append(item)
    return tuple(schedules)


def _schedule_from_section(
    article_id: int, title: str, kind: str, text: str
) -> OfficialSchedule | None:
    ranges = {(match["start"], match["end"], match["zone"]) for match in _PERIOD.finditer(text)}
    if len(ranges) != 1:
        return None
    start_text, end_text, zone_text = next(iter(ranges))
    zone: tzinfo | None = None
    if zone_text == "伺服器時間":
        zone = ASIA_ZONE
    elif zone_text:
        offset = re.search(r"([+\-\u2212])\s*(\d{1,2})(?::(\d{2}))?", zone_text)
        if offset:
            hours, minutes = int(offset[2]), int(offset[3] or 0)
            if hours <= 14 and minutes < 60:
                sign = 1 if offset[1] == "+" else -1
                zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
    start = _exact_moment(start_text, zone)
    end = _exact_moment(end_text, zone)
    if start and end and end < start:
        return None
    five: list[str] = []
    four: list[str] = []
    for rate in re.finditer(r"5星角色(?P<names>[^\uFF01!\n]*?)喚取機率限時提升", text):
        groups = re.split(r"4星角色", rate["names"], maxsplit=1)
        five.extend(re.findall(r"「([^」]+)」", groups[0]))
        if len(groups) == 2:
            four.extend(re.findall(r"「([^」]+)」", groups[1]))
    version_match = re.search(r"(\d+\.\d+)\s*版本更新後", start_text)
    bonus_kind = None
    daily_bonus_limit = None
    bonus_scopes: tuple[str, ...] = ()
    bonus_text = title + " " + text
    if re.search(r"雙倍|翻倍|加倍", bonus_text):
        kinds = [
            value
            for labels, value in (
                (("無音區", "無音清剿"), "tacet"),
                (("凝素領域",), "forgery"),
                (("模擬領域",), "simulation"),
            )
            if any(label in bonus_text for label in labels)
        ]
        if kinds:
            bonus_scopes = tuple(kinds)
            bonus_kind = kinds[0] if len(kinds) == 1 else "mixed"
            limit = re.search(r"每日[^。\n]{0,40}?(\d{1,2})\s*次", bonus_text)
            if limit and int(limit[1]) > 0:
                daily_bonus_limit = int(limit[1])
    identity = f"{kind}|{title}|{end.isoformat() if end else re.sub(r'\s+', '', end_text)}"
    return OfficialSchedule(
        key="official:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        title=title,
        kind=kind,
        official_url=official_article_url(article_id),
        starts_at=start,
        ends_at=end,
        five_star_characters=tuple(dict.fromkeys(five)),
        four_star_characters=tuple(dict.fromkeys(four)),
        start_label=start_text + ("\uff08Asia\uff09" if zone_text == "伺服器時間" else ""),
        end_label=end_text + ("\uff08Asia\uff09" if zone_text == "伺服器時間" else ""),
        version=version_match[1] if version_match else None,
        bonus_kind=bonus_kind,
        daily_bonus_limit=daily_bonus_limit,
        bonus_scopes=bonus_scopes,
    )


def _exact_moment(value: str, zone: tzinfo | None) -> datetime | None:
    if zone is None:
        return None
    numbers = re.fullmatch(
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:\uFF1A]\s*(\d{2})", value
    )
    if numbers is None:
        return None
    try:
        year, month, day, hour, minute = (int(number) for number in numbers.groups())
        moment = datetime(year, month, day, hour, minute, tzinfo=zone)
    except ValueError:
        return None
    return moment if moment.year < 2090 else None


__all__ = [
    "MenuArticle",
    "classify_notice",
    "notice_without_detail",
    "parse_article_notice",
    "parse_main_menu",
    "select_recent_candidates",
]
