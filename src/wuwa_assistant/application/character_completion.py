"""Adapter from persistent character snapshots to the pure completion engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wuwa_assistant.cultivation import (
    BlockerCode,
    CharacterCultivationInput,
    CultivationCompletion,
    CultivationRules,
    EchoSlotProgress,
    ProgressTarget,
    SkillPriority,
    SkillProgress,
    WeaponProgress,
    evaluate_cultivation,
)

from .characters import CharacterDetail, EchoSlotSnapshot


@dataclass(frozen=True, slots=True)
class CharacterCompletionView:
    """Completion result plus a concise deterministic next action."""

    completion: CultivationCompletion
    next_action: str


def evaluate_character_detail(
    detail: CharacterDetail,
    *,
    passing_score: float = 70.0,
    scoring_profile_id: str | None = None,
    scoring_profile_version: int | None = None,
) -> CharacterCompletionView:
    """Evaluate a detached database snapshot without guessing unknown echo fields."""

    goal = detail.cultivation_goal
    character_target = _positive_goal_int(goal, "character_level_target", 90)
    weapon_level_target = _positive_goal_int(goal, "weapon_level_target", 90)
    weapon_rank_target = _positive_goal_int(goal, "weapon_rank_target", 1)
    weapon = detail.weapon
    progress = CharacterCultivationInput(
        character_name=detail.summary.name,
        level=ProgressTarget(detail.summary.level, character_target),
        weapon=(
            None
            if weapon is None
            else WeaponProgress(
                name=weapon.name,
                level=ProgressTarget(weapon.level, weapon_level_target),
                rank=ProgressTarget(weapon.resonance_rank, weapon_rank_target),
            )
        ),
        skills=tuple(
            SkillProgress(
                name=skill.name,
                current_level=skill.current_level,
                priority=SkillPriority.CORE if skill.target_level >= 9 else SkillPriority.SECONDARY,
                target_level=skill.target_level,
            )
            for skill in detail.skills
        ),
        small_nodes=tuple(detail.small_stat_nodes),
        echo_slots=tuple(
            _echo_progress(
                slot,
                goal,
                scoring_profile_id=scoring_profile_id or detail.summary.scoring_profile_id,
                scoring_profile_version=scoring_profile_version,
            )
            for slot in detail.echo_slots
        ),
    )
    completion = evaluate_cultivation(
        progress,
        CultivationRules(echo_passing_score=passing_score),
    )
    return CharacterCompletionView(completion, _next_action(completion))


def _echo_progress(
    slot: EchoSlotSnapshot,
    goal: Mapping[str, Any],
    *,
    scoring_profile_id: str | None,
    scoring_profile_version: int | None,
) -> EchoSlotProgress:
    if slot.echo_id is None:
        return EchoSlotProgress(slot.slot_index, is_filled=False)
    score = slot.score
    if scoring_profile_id is not None and slot.score_profile_id != scoring_profile_id:
        score = None
    if (
        scoring_profile_version is not None
        and slot.score_profile_version != scoring_profile_version
    ):
        score = None
    return EchoSlotProgress(
        slot_index=slot.slot_index,
        is_filled=True,
        set_is_valid=_set_validity(slot, goal),
        main_stat_is_valid=_main_stat_validity(slot, goal),
        score=score,
        echo_id=slot.echo_id,
    )


def _set_validity(slot: EchoSlotSnapshot, goal: Mapping[str, Any]) -> bool | None:
    if not slot.set_name:
        return None
    confirmation = _slot_confirmation(goal, slot.slot_index, slot.echo_id, "set")
    if confirmation is not None:
        return confirmation
    expected = _slot_targets(goal.get("echo_set_targets"), slot.slot_index)
    return None if not expected else slot.set_name in expected


def _main_stat_validity(slot: EchoSlotSnapshot, goal: Mapping[str, Any]) -> bool | None:
    names = tuple(
        str(stat.get("name", "")).strip()
        for stat in slot.main_stats
        if isinstance(stat, Mapping)
        and stat.get("stat_kind") != "fixed"
        and str(stat.get("name", "")).strip()
    )
    if not names:
        return None
    confirmation = _slot_confirmation(goal, slot.slot_index, slot.echo_id, "main_stat")
    if confirmation is not None:
        return confirmation
    expected = _slot_targets(goal.get("echo_main_stat_targets"), slot.slot_index)
    return None if not expected else any(name in expected for name in names)


def _slot_confirmation(
    goal: Mapping[str, Any],
    slot_index: int,
    echo_id: str | None,
    key: str,
) -> bool | None:
    confirmations = goal.get("echo_slot_confirmations")
    if not isinstance(confirmations, Mapping):
        return None
    slot = confirmations.get(str(slot_index), confirmations.get(slot_index))
    if not isinstance(slot, Mapping):
        return None
    if echo_id is None or slot.get("echo_id") != echo_id:
        return None
    value = slot.get(key)
    return value if type(value) is bool else None


def _slot_targets(value: Any, slot_index: int) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    raw = value.get(str(slot_index), value.get(slot_index))
    if isinstance(raw, str):
        normalized = raw.strip()
        return (normalized,) if normalized else ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _positive_goal_int(goal: Mapping[str, Any], key: str, default: int) -> int:
    value = goal.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _next_action(completion: CultivationCompletion) -> str:
    if completion.at_goal:
        return "已達目前養成目標"
    if not completion.blockers:
        return "資料不足,需要人工確認"
    first = completion.blockers[0]
    if first.code is BlockerCode.ECHO_MISSING:
        return f"先補齊永久聲骸位置 {first.slot_index}"
    if first.code in {
        BlockerCode.ECHO_SET_UNKNOWN,
        BlockerCode.ECHO_MAIN_STAT_UNKNOWN,
        BlockerCode.ECHO_SCORE_MISSING,
    }:
        return f"確認永久聲骸位置 {first.slot_index} 的資料"
    if first.code in {
        BlockerCode.ECHO_SET_INVALID,
        BlockerCode.ECHO_MAIN_STAT_INVALID,
        BlockerCode.ECHO_SCORE_BELOW_THRESHOLD,
    }:
        return f"優先改善永久聲骸位置 {first.slot_index}"
    return first.message_zh_tw


__all__ = ["CharacterCompletionView", "evaluate_character_detail"]
