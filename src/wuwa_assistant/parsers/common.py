"""Shared strict field parsers for OCR tokens."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from wuwa_assistant.models import EchoStat, OcrToken, StatKind

from .lexicon import calibrated_stat_name_confidence, match_stat_name

MIN_TEXT_CONFIDENCE = 0.60
MIN_NUMBER_CONFIDENCE = 0.80

_NUMBER_RE = re.compile(r"^(?P<sign>[+]?)?(?P<number>\d{1,5}(?:\.\d)?)(?P<percent>%?)$")
_LEVEL_RE = re.compile(r"^\+?([0-9]|1[0-9]|2[0-5])$")


def token_center(token: OcrToken) -> tuple[float, float]:
    x1, y1, x2, y2 = token.bbox
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def tokens_in_band(tokens: Iterable[OcrToken], y1: float, y2: float) -> list[OcrToken]:
    return [token for token in tokens if y1 <= token_center(token)[1] < y2]


def strict_number(text: str) -> tuple[int | float, str | None] | None:
    normalized = unicodedata.normalize("NFKC", text).strip().replace(" ", "")
    match = _NUMBER_RE.fullmatch(normalized)
    if match is None:
        return None
    number_text = match.group("number")
    value: int | float = float(number_text) if "." in number_text else int(number_text)
    unit = "%" if match.group("percent") else None
    return value, unit


def strict_level(text: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", text).strip().replace(" ", "")
    match = _LEVEL_RE.fullmatch(normalized)
    return int(match.group(1)) if match else None


def parse_name(tokens: list[OcrToken], y1: float, y2: float) -> tuple[str | None, float | None]:
    candidates = []
    for token in tokens_in_band(tokens, y1, y2):
        if token.confidence < MIN_TEXT_CONFIDENCE:
            continue
        if re.search(r"[\u3400-\u9fff]", token.text):
            candidates.append(token)
    if not candidates:
        return None, None
    chosen = max(candidates, key=lambda token: (token.confidence, len(token.text)))
    return chosen.text.strip(), chosen.confidence


def parse_level_band(
    tokens: list[OcrToken], y1: float, y2: float
) -> tuple[int | None, float | None]:
    for token in sorted(tokens_in_band(tokens, y1, y2), key=lambda item: token_center(item)[0]):
        if token.confidence < MIN_NUMBER_CONFIDENCE:
            continue
        value = strict_level(token.text)
        if value is not None:
            return value, token.confidence
    return None, None


def parse_stat_row(
    tokens: list[OcrToken],
    *,
    y1: float,
    y2: float,
    stat_kind: StatKind,
    slot_index: int | None = None,
    value_split_x: float = 450,
) -> tuple[EchoStat | None, bool]:
    """Return (stat, locked-marker-seen). Invalid rows are neither guessed nor unlocked."""

    row = tokens_in_band(tokens, y1, y2)
    if any("強化至" in token.text and "啟用" in token.text for token in row):
        return None, True

    name_token: OcrToken | None = None
    vocabulary_match = None
    for token in row:
        if token_center(token)[0] >= value_split_x or token.confidence < MIN_TEXT_CONFIDENCE:
            continue
        match = match_stat_name(token.text)
        if match is not None and (
            vocabulary_match is None or match.similarity > vocabulary_match.similarity
        ):
            name_token = token
            vocabulary_match = match

    value_token: OcrToken | None = None
    parsed_value: tuple[int | float, str | None] | None = None
    for token in row:
        if token_center(token)[0] < value_split_x or token.confidence < MIN_NUMBER_CONFIDENCE:
            continue
        candidate = strict_number(token.text)
        if candidate is not None:
            value_token = token
            parsed_value = candidate
            break

    if (
        name_token is None
        or vocabulary_match is None
        or value_token is None
        or parsed_value is None
    ):
        return None, False

    value_confidence = value_token.confidence
    name_confidence = calibrated_stat_name_confidence(
        vocabulary_match,
        ocr_confidence=name_token.confidence,
        value_confidence=value_confidence,
    )
    value, unit = parsed_value
    return (
        EchoStat(
            stat_kind=stat_kind,
            name=vocabulary_match.value,
            value=value,
            unit=unit,
            confidence=min(name_confidence, value_confidence),
            name_confidence=name_confidence,
            value_confidence=value_confidence,
            raw_name=name_token.text,
            raw_value=value_token.text,
            slot_index=slot_index,
        ),
        False,
    )


def null_stat(stat_kind: StatKind, slot_index: int | None = None) -> EchoStat:
    return EchoStat(
        stat_kind=stat_kind,
        name=None,
        value=None,
        unit=None,
        confidence=None,
        name_confidence=None,
        value_confidence=None,
        slot_index=slot_index,
    )
