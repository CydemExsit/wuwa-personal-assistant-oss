"""Explainable farming priorities, separate from the existing Echo score formula.

These are opportunity points, not expected damage, drop probabilities or stamina
efficiency. Unknown observations are never converted into a zero Echo score.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import fsum, sqrt

POLICY_VERSION = "tacet-priority.v2"


class FarmRole(StrEnum):
    MAIN = "主輸出"
    SECONDARY = "副輸出"
    HEALER = "治療輔助"
    UNKNOWN = "定位待確認"


@dataclass(frozen=True, slots=True)
class FarmEcho:
    slot_index: int
    cost: int | None
    level: int | None
    score: float | None
    set_name: str | None
    set_valid: bool | None
    main_valid: bool | None
    equipped: bool = True
    target_sets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FarmCharacter:
    identifier: str
    name: str
    role: FarmRole
    role_reason: str
    sets: tuple[str, ...]
    echoes: tuple[FarmEcho, ...]
    set_combinations: tuple[tuple[tuple[str, int], ...], ...] = ()
    passing_score: float = 70.0
    energy_current: float | None = None
    energy_target: float | None = None
    priority: int = 100
    check_set_combination: bool = True
    best_team_priority: int | None = None


@dataclass(frozen=True, slots=True)
class FarmZone:
    identifier: str
    name: str
    region: str
    sets: tuple[str, ...]
    source_note: str = ""


@dataclass(frozen=True, slots=True)
class FarmBenefit:
    character_id: str
    character_name: str
    set_name: str
    points: float
    reason: str
    slot_index: int = 0
    current_score: float | None = None
    need_points: float = 0.0
    importance_multiplier: float = 1.0
    piece_multiplier: float = 1.0
    importance_reason: str = ""


@dataclass(frozen=True, slots=True)
class ZoneRecommendation:
    zone: FarmZone
    points: float
    benefits: tuple[FarmBenefit, ...]
    useful_sets: tuple[str, ...]
    weighted_base_points: float = 0.0
    coverage_multiplier: float = 1.0
    dual_set_multiplier: float = 1.0
    effective_character_count: float = 0.0

    @property
    def character_count(self) -> int:
        return len({benefit.character_id for benefit in self.benefits})

    @property
    def echo_count(self) -> int:
        return len({(benefit.character_id, benefit.slot_index) for benefit in self.benefits})


@dataclass(frozen=True, slots=True)
class FarmingPlan:
    recommendations: tuple[ZoneRecommendation, ...]
    notices: tuple[str, ...]
    policy_version: str = POLICY_VERSION


def resolve_role(role_text: str) -> FarmRole:
    """Recognize actual function, including hybrid damage/healing characters."""
    text = role_text.casefold().replace(" ", "")
    if any(token in text for token in ("副輸出", "副c", "subdps", "副攻")):
        return FarmRole.SECONDARY
    if any(token in text for token in ("主輸出", "主c", "maindps")):
        return FarmRole.MAIN
    if any(token in text for token in ("治療", "奶", "healer", "healing")):
        return FarmRole.HEALER
    if "輔助" in text and "輸出" in text:
        return FarmRole.SECONDARY
    if any(token in text for token in ("輸出", "dps")):
        return FarmRole.MAIN
    if any(token in text for token in ("輔助", "support")):
        return FarmRole.SECONDARY
    return FarmRole.UNKNOWN


def _quality_points(score: float, passing: float, role: FarmRole) -> float:
    gap = max(0.0, passing - score) / max(1.0, passing)
    if role is FarmRole.HEALER:
        return 0.0
    if role is FarmRole.SECONDARY:
        return 1.2 * gap if score < passing else 0.025 * (max(0, 100 - score) / 100) ** 2
    if role is FarmRole.MAIN:
        return 2.0 * gap + 0.65 * (max(0, 100 - score) / 100) ** 2
    return 0.0


def _priority_weights(priorities: set[int], *, spread: float) -> dict[int, float]:
    """Priorities are ordinal: equal values stay equal, not a ratio of arbitrary IDs."""
    ordered = sorted(priorities)
    if len(ordered) < 2:
        return dict.fromkeys(ordered, 1.0)
    return {
        priority: 1.0 + spread * (1.0 - rank / (len(ordered) - 1))
        for rank, priority in enumerate(ordered)
    }


def _rank_zone(zone: FarmZone, benefits: list[FarmBenefit]) -> ZoneRecommendation:
    matched = tuple(benefit for benefit in benefits if benefit.set_name in zone.sets)
    by_character: dict[str, dict[int, float]] = {}
    needs: dict[str, list[float]] = {}
    for benefit in matched:
        slots = by_character.setdefault(benefit.character_id, {})
        slots[benefit.slot_index] = slots.get(benefit.slot_index, 0.0) + benefit.points
        needs.setdefault(benefit.character_id, []).append(benefit.need_points)
    # A drop replaces one piece. Several weak slots still help, with diminishing
    # returns within a character; alternative sets share the same slot rank.
    discounts = {
        (character_id, slot): 1.0 / sqrt(rank + 1)
        for character_id, slots in by_character.items()
        for rank, (slot, _points) in enumerate(
            sorted(slots.items(), key=lambda item: (-item[1], item[0]))
        )
    }
    adjusted = tuple(
        replace(
            benefit,
            points=benefit.points * discounts[benefit.character_id, benefit.slot_index],
            piece_multiplier=discounts[benefit.character_id, benefit.slot_index],
        )
        for benefit in matched
    )
    base = fsum(benefit.points for benefit in adjusted)
    # Tiny gains on already-passing secondary characters must not get the same
    # coverage bonus as a genuine deficit merely by increasing the headcount.
    effective_count = fsum(min(1.0, fsum(values) / 0.75) for values in needs.values())
    coverage = 1.0 + 0.06 * min(4.0, max(0.0, effective_count - 1.0))
    set_points = tuple(
        fsum(benefit.points for benefit in matched if benefit.set_name == name)
        for name in dict.fromkeys(zone.sets)
    )
    # A bounded preference for balanced use of BOTH drops, not an assumed 50/50
    # drop rate. Use the pre-discount set needs: swapping two equal-need slots
    # between sets must not lower the bonus when their discount ranks exchange.
    # Both the additive bonus and its cap are monotone with growing need.
    dual_bonus = min(0.1 * base, 0.2 * min(set_points)) if len(set_points) == 2 else 0.0
    dual = 1.0 + (dual_bonus / base if base > 0 else 0.0)
    return ZoneRecommendation(
        zone=zone,
        points=round(base * coverage * dual, 4),
        benefits=adjusted,
        useful_sets=tuple(name for name in zone.sets if any(b.set_name == name for b in adjusted)),
        weighted_base_points=base,
        coverage_multiplier=coverage,
        dual_set_multiplier=dual,
        effective_character_count=effective_count,
    )


def recommend_zones(
    characters: tuple[FarmCharacter, ...], zones: tuple[FarmZone, ...]
) -> FarmingPlan:
    """Rank both sets using known needs, importance and useful coverage.

    A slot's points are divided over its compatible alternatives. A zone earns
    both shares when both sets are useful. The same character is evaluated once,
    even if it belongs to several teams. Cost-4 and uncertain-cost gaps remain
    explicit notices and cannot inflate a Tacet Field recommendation.
    """
    benefits: list[FarmBenefit] = []
    notices: list[str] = []
    seen: set[str] = set()
    available_sets = {name for zone in zones for name in zone.sets}
    priority_weights = _priority_weights({row.priority for row in characters}, spread=0.6)
    team_weights = _priority_weights(
        {row.best_team_priority for row in characters if row.best_team_priority is not None},
        spread=0.15,
    )
    for character in sorted(characters, key=lambda row: (row.priority, row.name, row.identifier)):
        if character.identifier in seen:
            continue
        seen.add(character.identifier)
        team_weight = (
            1.0
            if character.best_team_priority is None
            else 1.1 * team_weights[character.best_team_priority]
        )
        importance = priority_weights[character.priority] * team_weight
        if character.role is FarmRole.UNKNOWN:
            notices.append(f"{character.name}: 定位待確認, 暫不推測副詞條刷取收益。")
        if not character.sets:
            notices.append(f"{character.name}: 尚無已查核套裝目標, 請先在角色頁確認配裝。")
            continue
        for name in character.sets:
            if name not in available_sets:
                notices.append(f"{character.name}: {name} 尚無已查核無音區來源, 不列入刷取收益。")
        energy_gap = (
            character.energy_current is not None
            and character.energy_target is not None
            and character.energy_current < character.energy_target
        )
        if character.energy_target is not None and character.energy_current is None:
            notices.append(f"{character.name}: 建議共鳴效率 {character.energy_target:g}%。")
        if energy_gap:
            notices.append(
                f"{character.name}: 共鳴效率 {character.energy_current:g}% / "
                f"{character.energy_target:g}%, 先確認武器、主詞條與循環需求。"
            )
        counts: dict[str, int] = {}
        for echo in character.echoes:
            if echo.equipped and echo.set_name:
                counts[echo.set_name] = counts.get(echo.set_name, 0) + 1
        combinations = character.set_combinations or tuple(((name, 5),) for name in character.sets)
        best = min(
            combinations,
            key=lambda combo: sum(
                max(0, required - counts.get(name, 0)) for name, required in combo
            ),
        )
        deficient = (
            {name for name, required in best if counts.get(name, 0) < required}
            if character.check_set_combination
            else set()
        )
        surplus_slots: set[int] = set()
        if deficient:
            wanted = dict(best)
            for name, count in counts.items():
                excess = max(0, count - wanted.get(name, 0))
                candidates = sorted(
                    (echo for echo in character.echoes if echo.set_name == name),
                    key=lambda echo: (
                        echo.cost not in (1, 3),
                        echo.score is None,
                        echo.score if echo.score is not None else 100,
                        echo.slot_index,
                    ),
                )
                surplus_slots.update(echo.slot_index for echo in candidates[:excess])
        seen_slots: set[int] = set()
        for echo in character.echoes:
            if echo.slot_index in seen_slots:
                continue
            seen_slots.add(echo.slot_index)
            label = f"{character.name} 第 {echo.slot_index} 件"
            combination_gap = echo.slot_index in surplus_slots
            if echo.target_sets:
                targets = echo.target_sets
            elif combination_gap:
                targets = tuple(name for name in character.sets if name in deficient)
            elif echo.set_valid is True and echo.set_name is not None:
                targets = (echo.set_name,)
            else:
                targets = character.sets
            targets = tuple(dict.fromkeys(name for name in targets if name in available_sets))
            fit_wrong = echo.set_valid is False or echo.main_valid is False or combination_gap
            points = 0.0
            reasons: list[str] = []
            if echo.cost not in (1, 3):
                if (
                    fit_wrong
                    or not echo.equipped
                    or (echo.score is not None and echo.score < character.passing_score)
                ):
                    notices.append(
                        f"{label}: COST 4 請依首領/夢魘等實際來源取得, 無音區無法補齊。"
                        if echo.cost == 4
                        else f"{label}: COST 或裝備位置待確認, 未計入無音區收益。"
                    )
                continue
            if not echo.equipped or fit_wrong:
                points = 3.0 if character.role is FarmRole.MAIN else 2.0
                reasons.append(
                    "補齊整體套裝組合"
                    if combination_gap
                    else "補套裝/主詞條"
                    if echo.equipped
                    else "補齊空位"
                )
            elif echo.set_valid is None or echo.main_valid is None:
                notices.append(f"{label}: 套裝或主詞條待確認, 不把未知資料當作低分。")
                continue
            elif echo.level is None or echo.level < 25:
                notices.append(f"{label}: 先看現有聲骸強化潛力, 未開滿低累積分不等於要替換。")
                continue
            elif echo.score is None:
                if character.role is not FarmRole.HEALER:
                    notices.append(f"{label}: 分數待確認, 不把未知資料當作低分。")
                    continue
            else:
                points = _quality_points(echo.score, character.passing_score, character.role)
                if points:
                    reasons.append(
                        f"{echo.score:g} 分, 低於 {character.passing_score:g} 分門檻"
                        if echo.score < character.passing_score
                        else "主輸出持續精進(邊際收益遞減)"
                        if character.role is FarmRole.MAIN
                        else "副輸出已達標, 僅低優先備選"
                    )
            if energy_gap and echo.cost == 3 and echo.main_valid is True:
                points += 0.35
                reasons.append("必要共鳴效率待補(先核對現有裝備)")
            if not targets or points <= 0:
                continue
            for name in targets:
                benefits.append(
                    FarmBenefit(
                        character.identifier,
                        character.name,
                        name,
                        points * importance / len(targets),
                        f"{character.role} · 第 {echo.slot_index} 件: {'; '.join(reasons)}",
                        slot_index=echo.slot_index,
                        current_score=echo.score,
                        need_points=points / len(targets),
                        importance_multiplier=importance,
                        importance_reason=(
                            f"角色優先級 {character.priority}"
                            + (
                                " · 未設定隊伍"
                                if character.best_team_priority is None
                                else f" · 最優先隊伍 {character.best_team_priority}"
                            )
                        ),
                    )
                )
    recommendations = [_rank_zone(zone, benefits) for zone in zones]
    recommendations.sort(key=lambda row: (-row.points, -len(row.useful_sets), row.zone.identifier))
    return FarmingPlan(tuple(recommendations), tuple(dict.fromkeys(notices)))
