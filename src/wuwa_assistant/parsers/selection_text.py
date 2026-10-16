"""Strict header and set-effect labels from the Echo selection detail panel."""

from __future__ import annotations

import re
import unicodedata
from itertools import pairwise

from wuwa_assistant.models import OcrToken
from wuwa_assistant.models.names import canonical_character_name

from .common import (
    MIN_NUMBER_CONFIDENCE,
    MIN_TEXT_CONFIDENCE,
    strict_level,
    strict_number,
    token_center,
    tokens_in_band,
)

_NAME_LEVEL = re.compile(r"^(?P<name>.*[\u3400-\u9fff].*?)\s*\+\s*(?P<level>\d{1,2})$")
_SET_NAME = re.compile(r"[\u3400-\u9fff]+(?:·[\u3400-\u9fff]+)*")
_ECHO_NAME = re.compile(
    r"(?=.*[\u3400-\u9fff])[\u3400-\u9fffA-Za-z0-9]+(?:·[\u3400-\u9fffA-Za-z0-9]+)*"
)
_SET_COUNT = re.compile(r"^(?P<prefix>.*?)\((?P<count>[0-5])/(?P<total>[1-5])\)$")
_NAME_FRAGMENT = re.compile(r"[\u3400-\u9fffA-Za-z0-9·]+")


def _canonical_echo_name(text: str) -> str:
    name = canonical_character_name(text)
    # Measured from the supplied selection header: PP-OCRv6 confidently reads
    # the nightmare marker as 夢魔. Only normalize an explicit variant marker;
    # a bare base name, skill description or look-alike artwork is not evidence.
    if name.startswith("夢魔·"):
        return "夢魘·" + name[len("夢魔·") :]
    return name


def _header_name_tokens(tokens: list[OcrToken]) -> list[OcrToken]:
    names = []
    for token in tokens_in_band(tokens, 25, 100):
        text = unicodedata.normalize("NFKC", token.text).strip()
        match = _NAME_LEVEL.fullmatch(text)
        name = canonical_character_name(match.group("name") if match else text)
        if (
            1 <= len(name) <= 40
            and _NAME_FRAGMENT.fullmatch(name)
            and (not name.isascii() or re.fullmatch(r"[A-Za-z]+\d+", name))
        ):
            names.append(OcrToken(name, token.confidence, token.bbox))
    return sorted(names, key=lambda item: item.bbox[0])


def _parse_header_name(tokens: list[OcrToken]) -> tuple[str | None, float | None]:
    names = _header_name_tokens(tokens)
    if not names:
        return None, None
    if any(token.confidence < MIN_TEXT_CONFIDENCE for token in names):
        return None, None
    for left, right in pairwise(names):
        height = min(left.bbox[3] - left.bbox[1], right.bbox[3] - right.bbox[1])
        gap = right.bbox[0] - left.bbox[2]
        if (
            height <= 0
            or not -4 <= gap <= height * 1.5
            or abs(token_center(left)[1] - token_center(right)[1]) > height * 0.4
        ):
            # Multiple unrelated/overlapping names are ambiguous. Picking the
            # most confident fragment used to silently drop the variant prefix.
            return None, None
    name = "".join(token.text for token in names)
    if name.startswith("夢魘") and len(name) > 2 and name[2] != "·":
        name = "夢魘·" + name[2:]
    name = _canonical_echo_name(name)
    if not 2 <= len(name) <= 40 or _ECHO_NAME.fullmatch(name) is None:
        return None, None
    if re.search(r"\d", name) and not re.search(r"[A-Za-z]\d", name):
        # Bare trailing digits may be a level whose plus sign was missed.
        return None, None
    if name in {"夢魘", "夢魔"}:
        return None, None
    return name, min(token.confidence for token in names)


def selection_name_retry_bounds(tokens: list[OcrToken]) -> tuple[int, int, int, int] | None:
    """Recover a detector-dropped left prefix inside the already-owned header.

    The fixed header begins at panel x=95. A detected name starting far to its
    right can be only the base portion. A single recognition-only read includes
    that missing left area without another detector or any new frame capture.
    """

    names = _header_name_tokens(tokens)
    if not names or names[0].bbox[0] <= 130:
        return None
    return (95, 35, min(570, max(token.bbox[2] for token in names) + 6), 100)


def resolve_selection_name_retry(
    tokens: list[OcrToken], retry_tokens: list[OcrToken]
) -> tuple[str | None, float | None]:
    """Accept a complete same-header name; never remove positive prefix evidence."""

    original, confidence = _parse_header_name(tokens)
    retry, retry_confidence = _parse_header_name(retry_tokens)
    if retry is None or retry_confidence is None or retry_confidence < 0.85:
        # A known left gap still cannot certify a normal name when the reread
        # failed. Leave the name pending instead of storing the shortened name.
        return None, None
    if original is None:
        return retry, retry_confidence
    if retry == original:
        return original, min(confidence or 0.0, retry_confidence)
    if retry == "夢魘·" + original:
        return retry, min(confidence or 0.0, retry_confidence)
    return None, None


def has_selection_overlay(owner_tokens: list[OcrToken], action_tokens: list[OcrToken]) -> bool:
    """Reject the measured Windows screenshot notification covering game details."""

    app_name_seen = any(
        "".join(token.text.split()) in {"剪取與繪圖", "剪取工具"} and token.confidence >= 0.85
        for token in owner_tokens
    )
    notification_seen = any(
        "".join(token.text.split()).startswith(("儲存到剪貼簿", "在這裡選取來標"))
        and token.confidence >= 0.70
        for token in action_tokens
    )
    return app_name_seen and notification_seen


def parse_selection_cost(tokens: list[OcrToken]) -> tuple[int | None, float | None]:
    """Parse the explicit COST row, including the measured COST-3 separator artifact."""

    row = tokens_in_band(tokens, 90, 160)
    candidates: dict[int, float] = {}
    cost_anchor_confidence: float | None = None
    for token in row:
        if token.confidence < MIN_NUMBER_CONFIDENCE:
            continue
        normalized = unicodedata.normalize("NFKC", token.text).upper().replace(" ", "")
        match = re.fullmatch(r"COST-?([134])", normalized)
        if match:
            candidates[int(match.group(1))] = token.confidence
        if normalized == "COST":
            cost_anchor_confidence = token.confidence
    if cost_anchor_confidence is not None:
        for token in row:
            if token.confidence < MIN_NUMBER_CONFIDENCE:
                continue
            number = strict_number(token.text)
            if number is not None and number[1] is None and number[0] in {1, 3, 4}:
                candidates[int(number[0])] = min(cost_anchor_confidence, token.confidence)
    if len(candidates) == 1:
        return next(iter(candidates.items()))
    return None, None


def parse_selection_header(
    tokens: list[OcrToken],
) -> tuple[str | None, float | None, int | None, float | None]:
    """Separate an explicit trailing +level even when OCR merged it into a long name."""

    levels: dict[int, float] = {}
    for token in tokens_in_band(tokens, 25, 100):
        text = unicodedata.normalize("NFKC", token.text).strip()
        match = _NAME_LEVEL.fullmatch(text)
        level = strict_level(match.group("level")) if match is not None else strict_level(text)
        if level is not None and token.confidence >= MIN_NUMBER_CONFIDENCE:
            levels[level] = max(token.confidence, levels.get(level, 0.0))
    chosen_name, name_confidence = _parse_header_name(tokens)
    if len(levels) != 1:
        return chosen_name, name_confidence, None, None
    level, level_confidence = next(iter(levels.items()))
    return chosen_name, name_confidence, level, level_confidence


def parse_set_name(tokens: list[OcrToken]) -> tuple[str | None, float | None]:
    """Read names next to set-count markers below the explicit 合鳴效果 heading.

    Skill length and unlocked stat rows move this section vertically. Anchor and
    same-row count evidence locate the label without mistaking body text or skill
    names for a set. Multiple different set labels remain unknown.
    """

    anchors = [
        token
        for token in tokens
        if "".join(token.text.split()) == "合鳴效果" and token.confidence >= 0.80
    ]
    if len(anchors) != 1:
        return None, None
    anchor = anchors[0]
    below = [token for token in tokens if token_center(token)[1] > anchor.bbox[3]]
    candidates: dict[str, float] = {}
    for marker in below:
        normalized = canonical_character_name(marker.text)
        count = _SET_COUNT.fullmatch(normalized)
        if count is None or marker.confidence < 0.80:
            continue
        if int(count.group("count")) > int(count.group("total")):
            continue
        prefix = count.group("prefix")
        if _SET_NAME.fullmatch(prefix):
            name, confidence = prefix, marker.confidence
        else:
            neighbors = [
                token
                for token in below
                if token is not marker
                and token.confidence >= 0.85
                and token.bbox[0] < marker.bbox[0]
                and -20 <= marker.bbox[0] - token.bbox[2] <= 200
                and abs(token_center(token)[1] - token_center(marker)[1]) <= 12
            ]
            if len(neighbors) != 1:
                continue
            label = neighbors[0]
            name = canonical_character_name(label.text).rstrip("(")
            confidence = min(label.confidence, marker.confidence)
        if not 2 <= len(name) <= 30 or _SET_NAME.fullmatch(name) is None or confidence < 0.85:
            continue
        candidates[name] = max(min(confidence, anchor.confidence), candidates.get(name, 0.0))
    if len(candidates) != 1:
        return None, None
    return next(iter(candidates.items()))
