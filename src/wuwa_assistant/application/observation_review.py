"""Field-level review evidence shared by scoring, history and correction views.

OCR probabilities remain untouched. Explicit, audited user confirmation is stored
separately and only releases the specific field that the user inspected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite

from wuwa_assistant.scoring.data import load_roll_data
from wuwa_assistant.scoring.engine import canonical_stat_key
from wuwa_assistant.scoring.models import EchoScoreBreakdown


@dataclass(frozen=True, slots=True)
class FieldReviewIssue:
    field_path: str
    label: str
    reason: str

    @property
    def message(self) -> str:
        return f"{self.label}: {self.reason}"


def confirmed_fields(payload: Mapping[str, object]) -> frozenset[str]:
    value = payload.get("manual_confirmed_fields")
    return (
        frozenset(item for item in value if isinstance(item, str))
        if isinstance(value, list)
        else frozenset()
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(float(value)) else None


def field_confidence(
    stat: Mapping[str, object], part: str, path: str, confirmed: frozenset[str]
) -> float | None:
    if f"{path}/{part}" in confirmed:
        return 1.0
    value = _number(stat.get(f"{part}_confidence"))
    combined = _number(stat.get("confidence"))
    other = _number(stat.get("value_confidence" if part == "name" else "name_confidence"))
    if value is None:
        return combined
    if combined is not None and (other is None or combined < min(value, other)):
        return min(value, combined)
    return value


def scoring_stat_confidence(
    stat: Mapping[str, object], path: str, confirmed: frozenset[str]
) -> float | None:
    if not any(item.startswith(f"{path}/") for item in confirmed):
        values = [
            _number(stat.get(key)) for key in ("confidence", "name_confidence", "value_confidence")
        ]
        present = [value for value in values if value is not None]
        return min(present) if present else None
    name = field_confidence(stat, "name", path, confirmed)
    value = field_confidence(stat, "value", path, confirmed)
    return min(name, value) if name is not None and value is not None else None


def observation_review_issues(
    payload: Mapping[str, object],
    *,
    minimum_confidence: float = 0.85,
    requires_review: bool = False,
) -> tuple[FieldReviewIssue, ...]:
    """Explain missing, low confidence and impossible values, without inventing OCR."""

    issues: list[FieldReviewIssue] = []
    confirmed = confirmed_fields(payload)
    confidence = payload.get("confidence")
    confidence = confidence if isinstance(confidence, Mapping) else {}

    def add(path: str, label: str, reason: str) -> None:
        issues.append(FieldReviewIssue(path, label, reason))

    def check_confidence(
        path: str, label: str, value: float | None, *, required: bool = True
    ) -> None:
        if path in confirmed:
            return
        if value is None:
            if required:
                add(path, label, "缺少辨識信心, 請確認")
        elif value < minimum_confidence:
            add(path, label, f"辨識信心 {value:.1%}, 低於 {minimum_confidence:.0%}")

    name = payload.get("name")
    level = payload.get("level")
    for key, label, valid in (
        ("name", "聲骸名稱", isinstance(name, str) and bool(name.strip())),
        ("level", "等級", type(level) is int and 0 <= level <= 25),
        ("cost", "COST", type(payload.get("cost")) is int and payload.get("cost") in (1, 3, 4)),
    ):
        if not valid:
            add(f"/{key}", label, "未辨識或格式不正確")
        elif key in confidence:
            check_confidence(f"/{key}", label, _number(confidence.get(key)))
    if "page_type" in confidence:
        check_confidence("/page_type", "遊戲畫面", _number(confidence.get("page_type")))
    # These optional fields also participate in the parser's overall confidence.
    # Missing optional evidence is allowed; a reported low confidence must not be
    # silently cleared by confirming an unrelated name or stat.
    for key, label in (
        ("set_name", "合鳴套裝"),
        ("locked_substat_slots", "未開詞條數"),
        ("is_discarded", "淘汰標記"),
        ("is_equipped", "裝備狀態"),
        ("equipped_character_name", "裝備角色"),
        ("equipped_slot_index", "裝備位置"),
        ("projected_level", "預計等級"),
        ("material_source_selected", "素材選取狀態"),
        ("material_source_level", "素材等級"),
    ):
        value = _number(confidence.get(key))
        if value is not None:
            check_confidence(
                f"/{key}", label if key == "set_name" else f"{label} (請重新辨識)", value
            )

    rolls = load_roll_data()
    for collection, group in (("main_stats", "主屬性"), ("sub_stats", "副詞條")):
        raw = payload.get(collection)
        stats = raw if isinstance(raw, list) else []
        expected = (
            2
            if collection == "main_stats"
            else level // 5
            if type(level) is int and 0 <= level <= 25
            else None
        )
        if expected is not None and len(stats) != expected:
            add(f"/{collection}", group, f"應有 {expected} 條, 目前 {len(stats)} 條, 請重新辨識")
        keys: dict[object, list[int]] = {}
        for index, raw_stat in enumerate(stats):
            stat = raw_stat if isinstance(raw_stat, Mapping) else {}
            path, label = f"/{collection}/{index}", f"{group} {index + 1}"
            stat_name, value = stat.get("name"), _number(stat.get("value"))
            if not isinstance(stat_name, str) or not stat_name.strip():
                add(f"{path}/name", f"{label}名稱", "未辨識")
            else:
                check_confidence(
                    f"{path}/name",
                    f"{label}名稱 ({stat_name})",
                    field_confidence(stat, "name", path, confirmed),
                )
            if value is None:
                add(f"{path}/value", f"{label}數值", "未辨識或不是有效數字")
            else:
                check_confidence(
                    f"{path}/value",
                    f"{label}數值",
                    field_confidence(stat, "value", path, confirmed),
                )
            if collection != "sub_stats" or not isinstance(stat_name, str):
                continue
            unit = stat.get("unit")
            stat_key = canonical_stat_key(stat_name, unit if isinstance(unit, str) else None)
            if stat_key is None:
                known_name = any(canonical_stat_key(stat_name, u) is not None for u in ("", "%"))
                add(
                    f"{path}/unit" if known_name else f"{path}/name",
                    f"{label}名稱/單位",
                    f"無法對應「{stat_name}{unit or ''}」, 請確認名稱及 % 單位",
                )
            else:
                keys.setdefault(stat_key, []).append(index)
                allowed = rolls.values_for(stat_key) or ()
                tolerance = 0.51 if all(v.is_integer() for v in allowed) else 0.051
                if (
                    value is not None
                    and allowed
                    and min(abs(value - v) for v in allowed) > tolerance
                ):
                    add(
                        f"{path}/value",
                        f"{label}數值 ({stat_name})",
                        f"{value:g}{unit or ''} 不在已知檔位, 請核對數值/單位",
                    )
        for indices in keys.values():
            if len(indices) > 1:
                for index in indices:
                    add(
                        f"/{collection}/{index}/name",
                        f"{group} {index + 1}名稱",
                        "與其他副詞條重複, 請核對",
                    )
    if requires_review and not issues:
        add("/review", "整筆辨識", "仍標記為待覆核, 請重新辨識或確認欄位")
    return tuple(issues)


def hold_reviewed_score(
    breakdown: EchoScoreBreakdown, payload: Mapping[str, object], *, requires_review: bool
) -> EchoScoreBreakdown:
    """Keep valid per-stat lines, but never publish an unconfirmed overall score."""

    if not requires_review and not breakdown.requires_manual_confirmation:
        return breakdown
    issues = observation_review_issues(payload, requires_review=requires_review)
    context_labels = {
        "panel_crit_rate": "角色面板暴擊",
        "panel_crit_damage": "角色面板暴擊傷害",
        "current_totals.energy_regen": "角色面板共鳴效率",
    }
    configuration_notes = tuple(
        f"評分設定需要 {context_labels.get(key, key)}, 請補充角色面板資料"
        for key in breakdown.missing_context
    ) + tuple(
        f"評分設定缺少詞條權重: {key.removeprefix('profile_missing_weight:')}, 請修正設定檔"
        for key in breakdown.unknown_stats
        if key.startswith("profile_missing_weight:")
    )
    return replace(
        breakdown,
        score=None,
        band=None,
        opened_slot_quality_score=None,
        requires_manual_confirmation=True,
        notes=(*breakdown.notes, *(issue.message for issue in issues), *configuration_notes),
    )
