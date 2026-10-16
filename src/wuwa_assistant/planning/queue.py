"""Deterministic breadth-first account cultivation queue."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CharacterPlanningState,
    CultivationTaskProgress,
    ProgressionAction,
    ProgressionIssue,
    ProgressionPhase,
    ThresholdDirection,
)


@dataclass(frozen=True, slots=True)
class _UnrankedAction:
    action_id: str
    character_id: str
    character_name: str
    character_priority: int
    cultivation_target_id: str | None
    material_id: str | None
    phase: ProgressionPhase
    issue: ProgressionIssue
    target_key: str
    title: str
    reason: str
    requires_manual_confirmation: bool
    current_goal_reached: bool


def build_breadth_first_queue(
    characters: tuple[CharacterPlanningState, ...],
) -> tuple[ProgressionAction, ...]:
    """Rank account actions by progression phase before per-character preference.

    Unknown values are surfaced as confirmation actions; they are never treated
    as invalid, complete or passing. Archived/non-candidate characters are omitted.
    """

    character_ids = [item.character_id.casefold() for item in characters]
    if len(character_ids) != len(set(character_ids)):
        raise ValueError("character ids must be unique")

    actions = [
        action
        for character in characters
        if character.is_candidate
        for action in _character_actions(character)
    ]
    actions.sort(key=_sort_key)
    return tuple(_ranked_action(index, action) for index, action in enumerate(actions, start=1))


def _character_actions(character: CharacterPlanningState) -> list[_UnrankedAction]:
    actions: list[_UnrankedAction] = []
    supplied = {slot.slot_index: slot for slot in character.echo_slots}
    for slot_index in range(1, character.required_echo_slots + 1):
        slot = supplied.get(slot_index)
        target_key = f"echo-slot-{slot_index}"
        if slot is None or not slot.is_filled:
            actions.append(
                _action(
                    character,
                    ProgressionPhase.MISSING_ECHO,
                    ProgressionIssue.ECHO_SLOT_MISSING,
                    target_key,
                    f"補齊 {character.character_name} 的聲骸槽位 {slot_index}",
                    f"永久聲骸槽位 {slot_index} 尚未裝備; 先補齊五件配置。",
                    cultivation_target_id=None if slot is None else slot.cultivation_target_id,
                    material_id=None if slot is None else slot.material_id,
                )
            )
            continue
        if slot.set_is_valid is False or slot.main_stat_is_valid is False:
            invalid_parts = []
            if slot.set_is_valid is False:
                invalid_parts.append("套裝")
            if slot.main_stat_is_valid is False:
                invalid_parts.append("主詞條")
            actions.append(
                _action(
                    character,
                    ProgressionPhase.INVALID_ECHO,
                    ProgressionIssue.ECHO_SET_OR_MAIN_INVALID,
                    target_key,
                    f"更換 {character.character_name} 的聲骸槽位 {slot_index}",
                    f"槽位 {slot_index} 的{'及'.join(invalid_parts)}不符合養成目標。",
                    cultivation_target_id=slot.cultivation_target_id,
                    material_id=slot.material_id,
                )
            )
            continue
        if slot.set_is_valid is None or slot.main_stat_is_valid is None:
            actions.append(
                _action(
                    character,
                    ProgressionPhase.CONFIRMATION,
                    ProgressionIssue.ECHO_VALIDITY_UNKNOWN,
                    target_key,
                    f"確認 {character.character_name} 的聲骸槽位 {slot_index}",
                    f"槽位 {slot_index} 的套裝或主詞條尚未確認, 不會假設其正確或錯誤。",
                    requires_confirmation=True,
                    cultivation_target_id=slot.cultivation_target_id,
                    material_id=slot.material_id,
                )
            )
            continue
        if slot.score is None:
            actions.append(
                _action(
                    character,
                    ProgressionPhase.CONFIRMATION,
                    ProgressionIssue.ECHO_SCORE_UNKNOWN,
                    target_key,
                    f"確認 {character.character_name} 的聲骸槽位 {slot_index} 分數",
                    f"槽位 {slot_index} 尚無可用評分, 不會推測其強弱。",
                    requires_confirmation=True,
                    cultivation_target_id=slot.cultivation_target_id,
                    material_id=slot.material_id,
                )
            )

    for threshold in character.functional_thresholds:
        target_key = f"threshold-{threshold.key}"
        if threshold.at_goal is None:
            actions.append(
                _action(
                    character,
                    ProgressionPhase.CONFIRMATION,
                    ProgressionIssue.FUNCTIONAL_THRESHOLD_UNKNOWN,
                    target_key,
                    f"確認 {character.character_name} 的{threshold.label}",
                    f"{threshold.label}目前值未知, 無法判斷是否達到功能門檻。",
                    requires_confirmation=True,
                    cultivation_target_id=threshold.cultivation_target_id,
                    material_id=threshold.material_id,
                )
            )
        elif not threshold.at_goal:
            unit = threshold.unit or ""
            comparison = "至少" if threshold.direction is ThresholdDirection.AT_LEAST else "至多"
            actions.append(
                _action(
                    character,
                    ProgressionPhase.FUNCTIONAL_THRESHOLD,
                    ProgressionIssue.FUNCTIONAL_THRESHOLD_UNMET,
                    target_key,
                    f"補足 {character.character_name} 的{threshold.label}",
                    (
                        f"目前 {threshold.current:g}{unit}, "
                        f"目標{comparison} {threshold.target:g}{unit}。"
                    ),
                    cultivation_target_id=threshold.cultivation_target_id,
                    material_id=threshold.material_id,
                )
            )

    weak_slots = [
        slot
        for slot in character.echo_slots
        if slot.is_filled
        and slot.set_is_valid is True
        and slot.main_stat_is_valid is True
        and slot.score is not None
        and slot.score < character.echo_passing_score
    ]
    if weak_slots:
        weakest = min(weak_slots, key=lambda item: (item.score or 0.0, item.slot_index))
        assert weakest.score is not None
        actions.append(
            _action(
                character,
                ProgressionPhase.WEAKEST_ECHO,
                ProgressionIssue.WEAKEST_ECHO,
                f"echo-slot-{weakest.slot_index}",
                f"改善 {character.character_name} 的最弱聲骸",
                (
                    f"槽位 {weakest.slot_index} 分數 {weakest.score:g}, "
                    f"低於及格門檻 {character.echo_passing_score:g}。"
                ),
                cultivation_target_id=weakest.cultivation_target_id,
                material_id=weakest.material_id,
            )
        )

    actions.extend(
        _task_actions(
            character,
            character.weapon_tasks,
            issue=ProgressionIssue.WEAPON_INCOMPLETE,
            kind="weapon",
            title_prefix="完成武器",
        )
    )
    actions.extend(
        _task_actions(
            character,
            character.skill_tasks,
            issue=ProgressionIssue.SKILL_INCOMPLETE,
            kind="skill",
            title_prefix="完成技能",
        )
    )
    actions.extend(
        _task_actions(
            character,
            character.cultivation_tasks,
            issue=ProgressionIssue.CULTIVATION_TASK_INCOMPLETE,
            kind="cultivation",
            title_prefix="完成養成",
        )
    )
    return actions


def _task_actions(
    character: CharacterPlanningState,
    tasks: tuple[CultivationTaskProgress, ...],
    *,
    issue: ProgressionIssue,
    kind: str,
    title_prefix: str,
) -> list[_UnrankedAction]:
    actions: list[_UnrankedAction] = []
    for task in tasks:
        if task.at_goal is True:
            continue
        unknown = task.at_goal is None
        reason = (
            f"{task.label}目前值未知, 需要人工確認。"
            if unknown
            else f"{task.label}目前 {task.current}/{task.target}, 尚未達到目標。"
        )
        actions.append(
            _action(
                character,
                ProgressionPhase.CONFIRMATION if unknown else ProgressionPhase.SKILL_OR_WEAPON,
                issue,
                f"{kind}-{task.key}",
                f"{title_prefix}: {character.character_name}/{task.label}",
                reason,
                requires_confirmation=unknown,
                cultivation_target_id=task.cultivation_target_id,
                material_id=task.material_id,
            )
        )
    return actions


def _action(
    character: CharacterPlanningState,
    phase: ProgressionPhase,
    issue: ProgressionIssue,
    target_key: str,
    title: str,
    reason: str,
    *,
    requires_confirmation: bool = False,
    cultivation_target_id: str | None = None,
    material_id: str | None = None,
) -> _UnrankedAction:
    return _UnrankedAction(
        action_id=f"{character.character_id}:{issue.value}:{target_key}",
        character_id=character.character_id,
        character_name=character.character_name,
        character_priority=character.user_priority,
        cultivation_target_id=cultivation_target_id,
        material_id=material_id,
        phase=phase,
        issue=issue,
        target_key=target_key,
        title=title,
        reason=reason,
        requires_manual_confirmation=requires_confirmation,
        current_goal_reached=character.is_current_goal_reached,
    )


def _sort_key(action: _UnrankedAction) -> tuple[object, ...]:
    issue_order = {
        ProgressionIssue.WEAPON_INCOMPLETE: 0,
        ProgressionIssue.SKILL_INCOMPLETE: 1,
        ProgressionIssue.CULTIVATION_TASK_INCOMPLETE: 2,
    }
    return (
        action.phase,
        action.current_goal_reached,
        action.character_priority,
        action.character_name.casefold(),
        action.character_id.casefold(),
        issue_order.get(action.issue, 0),
        action.target_key.casefold(),
        action.action_id.casefold(),
    )


def _ranked_action(rank: int, action: _UnrankedAction) -> ProgressionAction:
    return ProgressionAction(
        rank=rank,
        action_id=action.action_id,
        character_id=action.character_id,
        character_name=action.character_name,
        character_priority=action.character_priority,
        cultivation_target_id=action.cultivation_target_id,
        material_id=action.material_id,
        phase=action.phase,
        issue=action.issue,
        target_key=action.target_key,
        title=action.title,
        reason=action.reason,
        requires_manual_confirmation=action.requires_manual_confirmation,
        current_goal_reached=action.current_goal_reached,
    )


__all__ = ["build_breadth_first_queue"]
