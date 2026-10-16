"""Validated public redemption-code records; never account redemption state."""

from __future__ import annotations

import re
from dataclasses import dataclass

CODE_PATTERN = re.compile(r"[A-Za-z0-9]{6,40}")
COMMUNITY_URL = "https://www.pockettactics.com/wuthering-waves/codes"


def validate_source(url: str) -> None:
    if url == COMMUNITY_URL:
        return
    if not re.fullmatch(
        r"https://wutheringwaves\.kurogames\.com/zh-tw/main/news/detail/[1-9][0-9]{0,9}",
        url,
    ):
        raise ValueError("invalid redemption-code source")


@dataclass(frozen=True, slots=True)
class RedeemCode:
    code: str
    source_url: str
    status: str = "unconfirmed"

    def __post_init__(self) -> None:
        if not CODE_PATTERN.fullmatch(self.code) or not any(c.isalpha() for c in self.code):
            raise ValueError("invalid redemption code")
        validate_source(self.source_url)
        if self.status not in {"listed", "unconfirmed", "expired", "unlisted"}:
            raise ValueError("invalid redemption-code status")

    @property
    def key(self) -> str:
        return self.code.upper()

    @property
    def available(self) -> bool:
        return self.status in {"listed", "unconfirmed"}


@dataclass(frozen=True, slots=True)
class RedeemSnapshot:
    codes: tuple[RedeemCode, ...] = ()
    checked_at: str | None = None
    message: str = "尚未檢查兌換碼。"
    fresh: bool = False
    checked_codes: tuple[str, ...] = ()
