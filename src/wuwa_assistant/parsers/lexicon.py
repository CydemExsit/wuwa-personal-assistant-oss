"""Traditional Chinese stat vocabulary and conservative fuzzy matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

STAT_NAMES: tuple[str, ...] = (
    "共鳴解放傷害加成",
    "共鳴技能傷害加成",
    "普攻傷害加成",
    "重擊傷害加成",
    "治療效果加成",
    "熱熔傷害加成",
    "冷凝傷害加成",
    "導電傷害加成",
    "氣動傷害加成",
    "衍射傷害加成",
    "湮滅傷害加成",
    "暴擊傷害",
    "共鳴效率",
    "暴擊",
    "生命",
    "攻擊",
    "防禦",
)

_DECORATION_RE = re.compile(r"[\s+*★✦✧·•,\uff0c.。:\uff1a、_\-/\\]+")
_KNOWN_ICON_PREFIXES = frozenset({"X", "x", "十"})


class VocabularyEvidence(StrEnum):
    """How an OCR token was mapped to one canonical stat name."""

    EXACT = "exact"
    DECORATED_EXACT = "decorated_exact"
    KNOWN_ICON_PREFIX = "known_icon_prefix"
    FUZZY = "fuzzy"


@dataclass(frozen=True, slots=True)
class VocabularyMatch:
    value: str
    similarity: float
    runner_up_similarity: float
    evidence: VocabularyEvidence


def normalize_ui_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return _DECORATION_RE.sub("", normalized)


def match_stat_name(text: str, *, minimum: float = 0.72) -> VocabularyMatch | None:
    normalized = normalize_ui_text(text)
    if not normalized:
        return None

    raw_nfkc = unicodedata.normalize("NFKC", text)
    scored: list[tuple[float, str, VocabularyEvidence]] = []
    for candidate in STAT_NAMES:
        if normalized == candidate:
            score = 1.0
            evidence = (
                VocabularyEvidence.EXACT
                if raw_nfkc == candidate
                else VocabularyEvidence.DECORATED_EXACT
            )
        elif normalized.endswith(candidate) and normalized[: -len(candidate)] in (
            _KNOWN_ICON_PREFIXES
        ):
            # In the measured Traditional-Chinese UI samples, the stat-row star
            # is occasionally recognized as X or 十.  Keep this allowlist
            # explicit; an arbitrary prefix must fall through to fuzzy matching
            # and cannot inherit strong suffix evidence.
            score = 0.90
            evidence = VocabularyEvidence.KNOWN_ICON_PREFIX
        else:
            score = SequenceMatcher(a=normalized, b=candidate).ratio()
            evidence = VocabularyEvidence.FUZZY
        scored.append((score, candidate, evidence))
    scored.sort(reverse=True)
    best_score, best_value, best_evidence = scored[0]
    runner_up = scored[1][0]
    if best_score < minimum:
        return None
    if best_score < 0.90 and best_score - runner_up < 0.12:
        return None
    return VocabularyMatch(best_value, best_score, runner_up, best_evidence)


def calibrated_stat_name_confidence(
    match: VocabularyMatch,
    *,
    ocr_confidence: float,
    value_confidence: float,
) -> float:
    """Combine lexical and paired-number evidence without trusting fuzzy text.

    Decorative glyphs are part of the game UI rather than the stat label and
    measurably lower RapidOCR's line confidence.  A small calibration is only
    applied when all of the following are true:

    * normalization leaves an exact known label, or removes one measured icon
      prefix;
    * the raw text still has at least 0.70 OCR confidence;
    * the number paired on the same row has at least 0.95 confidence.

    Fuzzy, ambiguous, weak-text, and weak-number evidence keeps the original
    conservative minimum and therefore remains below downstream review gates.
    """

    base = min(ocr_confidence, match.similarity)
    if ocr_confidence < 0.70 or value_confidence < 0.95:
        return base
    if match.evidence not in {
        VocabularyEvidence.DECORATED_EXACT,
        VocabularyEvidence.KNOWN_ICON_PREFIX,
    }:
        return base

    calibrated = 1.0 - (1.0 - ocr_confidence) * 0.5
    evidence_cap = (
        0.95 if match.evidence is VocabularyEvidence.DECORATED_EXACT else match.similarity
    )
    return min(value_confidence, evidence_cap, calibrated)
