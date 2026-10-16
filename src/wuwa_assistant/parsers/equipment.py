"""Exact, local owner-label parsing without a character catalog or name guessing."""

from __future__ import annotations

import re

from wuwa_assistant.models import OcrToken
from wuwa_assistant.models.names import canonical_character_name

MIN_EQUIPPED_TEXT_CONFIDENCE = 0.70
MIN_EQUIPPED_OWNER_CONFIDENCE = 0.85
_EQUIPPED_SUFFIXES = ("裝配中", "裝備中", "装配中", "装备中")
_CHARACTER_NAME = re.compile(r"[\u3400-\u9fff]+(?:[·・][\u3400-\u9fff]+)*")


def _adjacent(left: OcrToken, right: OcrToken) -> bool:
    """Require neighboring fragments to share a baseline and a short text gap."""

    lx1, ly1, lx2, ly2 = left.bbox
    rx1, ry1, rx2, ry2 = right.bbox
    height = min(ly2 - ly1, ry2 - ry1)
    return (
        lx2 > lx1
        and rx2 > rx1
        and height > 0
        and min(ly2, ry2) - max(ly1, ry1) >= 0.60 * height
        and -2 <= rx1 - lx2 <= max(10, height * 0.75)
    )


def equipped_owner_lines(tokens: list[OcrToken]) -> list[tuple[str, float]]:
    """Join only contiguous OCR fragments; retain weak fragments to avoid truncation."""

    groups: list[list[OcrToken]] = []
    for token in sorted(tokens, key=lambda item: (item.bbox[0], item.bbox[1])):
        if not token.text.strip():
            continue
        neighboring = [group for group in groups if _adjacent(group[-1], token)]
        if len(neighboring) == 1:
            neighboring[0].append(token)
        else:
            groups.append([token])
    return [
        (
            canonical_character_name("".join(token.text for token in group)),
            min(token.confidence for token in group),
        )
        for group in groups
    ]


def equipped_suffix_confidence(lines: list[tuple[str, float]]) -> float | None:
    evidence = [
        confidence
        for text, confidence in lines
        if confidence >= MIN_EQUIPPED_TEXT_CONFIDENCE and text.endswith(_EQUIPPED_SUFFIXES)
    ]
    return max(evidence) if evidence else None


def parse_equipped_owner(lines: list[tuple[str, float]]) -> tuple[str | None, float | None]:
    """Extract visible Chinese owner text, including names absent from local seed data.

    The complete name and suffix must be strong OCR. A badge, a suffix alone,
    invalid text, competing names, or a partial name never creates an identity.
    No fuzzy spelling correction or simplified/traditional substitution is used.
    """

    candidates: dict[str, float] = {}
    for text, confidence in lines:
        if confidence < MIN_EQUIPPED_OWNER_CONFIDENCE:
            continue
        suffix = next((suffix for suffix in _EQUIPPED_SUFFIXES if text.endswith(suffix)), None)
        if suffix is None:
            continue
        name = text[: -len(suffix)]
        if not 1 <= len(name) <= 20 or _CHARACTER_NAME.fullmatch(name) is None:
            continue
        if any(marker in name for marker in (*_EQUIPPED_SUFFIXES, "未裝", "未装")):
            continue
        candidates[name] = max(confidence, candidates.get(name, 0.0))
    if len(candidates) != 1:
        return None, None
    return next(iter(candidates.items()))
