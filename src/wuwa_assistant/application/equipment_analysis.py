"""Local, evidence-bound explanation of recorded equipment and replacement needs."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from wuwa_assistant.scoring import CharacterScoringProfile, EchoScoreBreakdown, canonical_stat_key
from wuwa_assistant.scoring.character_guides import STAT_LABELS, CharacterGuide

MAIN_LABELS = {str(key): label for key, label in STAT_LABELS.items()} | {
    "healing_bonus": "治療效果加成",
    "glacio_damage": "冷凝傷害加成",
    "fusion_damage": "熱熔傷害加成",
    "aero_damage": "氣動傷害加成",
    "havoc_damage": "湮滅傷害加成",
    "spectro_damage": "衍射傷害加成",
    "electro_damage": "導電傷害加成",
}


@dataclass(frozen=True, slots=True)
class EchoStatDetail:
    name: str
    value: float | int | None
    unit: str
    relevance: str
    contribution: float | None
    weight: float | None = None
    roll_quality: str = ""


@dataclass(frozen=True, slots=True)
class EchoTrainingRecommendation:
    action: str
    reason: str
    target_sets: tuple[str, ...] = ()
    target_main_stats: tuple[str, ...] = ()
    target_substats: tuple[str, ...] = ()


def primary_main_key(stats: tuple[dict[str, Any], ...]) -> str | None:
    primary = next((s for s in stats if s.get("stat_kind") == "primary"), None)
    if primary is None:
        # A single recorded line can be primary; never accept a fixed secondary
        # attack/HP line as evidence for a selectable main stat.
        primary = next((s for s in stats if s.get("stat_kind") != "fixed"), None)
    if primary is None:
        return None
    confidence = primary.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < 0.85:
        return None
    name, unit = str(primary.get("name") or ""), str(primary.get("unit") or "")
    key = canonical_stat_key(name, unit)
    if key is not None:
        return str(key)
    return next((k for k, label in MAIN_LABELS.items() if label == name), None)


def guide_fit(
    guide: CharacterGuide | None,
    *,
    cost: int | None,
    set_name: str | None,
    main_stats: tuple[dict[str, Any], ...],
) -> tuple[bool | None, bool | None, str, str]:
    if guide is None:
        return None, None, "尚無此角色的配裝參考", "尚無此角色的配裝參考"
    keys = guide.main_keys(cost)
    actual = primary_main_key(main_stats)
    set_fit = (
        None
        if not set_name or not guide.allowed_sets
        else guide.canonical_set_name(set_name) in guide.allowed_sets
    )
    main_fit = None if actual is None or not keys else actual in keys
    return (
        set_fit,
        main_fit,
        "推薦套裝" if set_fit is True else "與所選攻略不同" if set_fit is False else "套裝未辨識",
        "符合攻略方向"
        if main_fit is True
        else "與所選攻略不同"
        if main_fit is False
        else "主詞條未辨識",
    )


def explain_stats(
    stats: tuple[dict[str, Any], ...],
    breakdown: EchoScoreBreakdown | None,
    profile: CharacterScoringProfile | None,
    reliable: bool,
) -> tuple[EchoStatDetail, ...]:
    lines = {} if breakdown is None else {str(line.stat): line for line in breakdown.lines}
    counts = Counter(canonical_stat_key(s.get("name"), s.get("unit")) for s in stats)
    result = []
    for stat in stats:
        name, unit = str(stat.get("name") or "未辨識詞條"), str(stat.get("unit") or "")
        key = canonical_stat_key(name, unit)
        weight = None if key is None or profile is None else profile.weight_for(key)
        line = None if key is None or counts[key] != 1 else lines.get(str(key))
        contribution = None
        if line is not None and breakdown is not None and breakdown.maximum_weight_points:
            denominator = max(breakdown.maximum_weight_points, breakdown.earned_weight_points)
            contribution = round(line.weighted_points / denominator * 100, 2)
        value = stat.get("value")
        result.append(
            EchoStatDetail(
                name=name,
                value=value if type(value) in (float, int) else None,
                unit=unit,
                relevance=(
                    "尚未計分"
                    if weight is None
                    else "不加分"
                    if weight.weight == 0
                    else "核心"
                    if weight.weight >= 0.8
                    else "有用"
                    if weight.weight >= 0.4
                    else "次要"
                ),
                contribution=contribution,
                weight=None if weight is None else weight.weight,
                roll_quality="" if line is None else line.roll_tier.label_zh_tw,
            )
        )
    # Present actual normalized contributions, including clipping and rounding,
    # so adding visible rows reproduces the score rather than an unrelated sum.
    if reliable and breakdown is not None and breakdown.score is not None:
        total = sum(r.contribution or 0 for r in result)
        if total > 0:
            from dataclasses import replace

            scaled = [round((r.contribution or 0) / total * breakdown.score, 2) for r in result]
            last = max(i for i, r in enumerate(result) if r.contribution)
            scaled[last] = round(scaled[last] + breakdown.score - sum(scaled), 2)
            result = [
                replace(r, contribution=scaled[i] if r.contribution is not None else None)
                for i, r in enumerate(result)
            ]
    return tuple(result)


def training_advice(
    *,
    echo_id: str | None,
    level: int | None,
    score: float | None,
    passing: float,
    set_fit: bool | None,
    main_fit: bool | None,
    cost: int | None,
    guide: CharacterGuide | None,
    profile: CharacterScoringProfile | None,
) -> EchoTrainingRecommendation:
    if echo_id is None:
        action, reason = "補齊位置", "此位置尚未有可追溯的裝備紀錄"
    elif set_fit is False or main_fit is False:
        action, reason = "檢查配裝方向", "先找符合套裝與主詞條的胚子, 再比較副詞條"
    elif score is None:
        action, reason = "補齊辨識資料", "目前資料不足以比較, 不代表這件聲骸低分"
    elif level is not None and level < 25:
        action, reason = "先看強化潛力", f"目前 +{level} 尚未開滿詞條, 低累積分不等於應替換"
    elif score < passing:
        action, reason = "優先找替換", "已開滿詞條, 優先補核心詞條或提高有用詞條的數值"
    else:
        action, reason = "可先保留", "目前副詞條已達這份設定的門檻, 可先處理更弱的位置"
    return EchoTrainingRecommendation(
        action,
        reason,
        () if guide is None else guide.allowed_sets,
        () if guide is None else tuple(MAIN_LABELS.get(k, k) for k in guide.main_keys(cost)),
        ()
        if profile is None
        else tuple(
            STAT_LABELS[w.stat]
            for w in sorted(profile.weights, key=lambda w: -w.weight)
            if w.weight > 0
        )[:5],
    )
