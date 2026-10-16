from __future__ import annotations

import pytest

from wuwa_assistant.planning import (
    CharacterPlanningState,
    CultivationTaskProgress,
    FunctionalThreshold,
    PlanningEchoSlot,
    ProgressionIssue,
    ProgressionPhase,
    ThresholdDirection,
    build_breadth_first_queue,
)


def _echo(
    slot_index: int,
    *,
    filled: bool = True,
    set_valid: bool | None = True,
    main_valid: bool | None = True,
    score: float | None = 75,
    target_id: str | None = None,
    material_id: str | None = None,
) -> PlanningEchoSlot:
    if not filled:
        return PlanningEchoSlot(
            slot_index,
            False,
            cultivation_target_id=target_id,
            material_id=material_id,
        )
    return PlanningEchoSlot(
        slot_index,
        True,
        set_valid,
        main_valid,
        score,
        target_id,
        material_id,
    )


def _character(
    character_id: str,
    *,
    name: str | None = None,
    priority: int = 100,
    candidate: bool = True,
    reached: bool = False,
    echoes: tuple[PlanningEchoSlot, ...] | None = None,
    thresholds: tuple[FunctionalThreshold, ...] = (),
    weapon_tasks: tuple[CultivationTaskProgress, ...] = (),
    skill_tasks: tuple[CultivationTaskProgress, ...] = (),
    cultivation_tasks: tuple[CultivationTaskProgress, ...] = (),
) -> CharacterPlanningState:
    return CharacterPlanningState(
        character_id=character_id,
        character_name=name or character_id,
        user_priority=priority,
        is_candidate=candidate,
        is_current_goal_reached=reached,
        echo_slots=echoes or tuple(_echo(index) for index in range(1, 6)),
        functional_thresholds=thresholds,
        weapon_tasks=weapon_tasks,
        skill_tasks=skill_tasks,
        cultivation_tasks=cultivation_tasks,
    )


def test_breadth_first_phases_override_character_priority() -> None:
    invalid_slots = list(_character("invalid").echo_slots)
    invalid_slots[1] = _echo(2, set_valid=False)
    missing_slots = tuple(_echo(index) for index in range(1, 5))
    threshold = FunctionalThreshold("er", "共鳴效率", 110, 130, "%")
    weak_slots = list(_character("weak").echo_slots)
    weak_slots[4] = _echo(5, score=40)
    characters = (
        _character(
            "tasks",
            priority=0,
            weapon_tasks=(CultivationTaskProgress("level", "武器等級", 80, 90),),
        ),
        _character("weak", priority=0, echoes=tuple(weak_slots)),
        _character("threshold", priority=0, thresholds=(threshold,)),
        _character("missing", priority=0, echoes=missing_slots),
        _character("invalid", priority=999, echoes=tuple(invalid_slots)),
    )

    queue = build_breadth_first_queue(characters)

    assert [action.issue for action in queue] == [
        ProgressionIssue.ECHO_SET_OR_MAIN_INVALID,
        ProgressionIssue.ECHO_SLOT_MISSING,
        ProgressionIssue.FUNCTIONAL_THRESHOLD_UNMET,
        ProgressionIssue.WEAKEST_ECHO,
        ProgressionIssue.WEAPON_INCOMPLETE,
    ]
    assert [action.phase for action in queue] == [
        ProgressionPhase.INVALID_ECHO,
        ProgressionPhase.MISSING_ECHO,
        ProgressionPhase.FUNCTIONAL_THRESHOLD,
        ProgressionPhase.WEAKEST_ECHO,
        ProgressionPhase.SKILL_OR_WEAPON,
    ]
    assert [action.rank for action in queue] == [1, 2, 3, 4, 5]


def test_unknown_fields_are_confirmation_actions_and_never_guessed() -> None:
    slots = list(_character("unknown").echo_slots)
    slots[0] = _echo(
        1,
        set_valid=None,
        score=None,
        target_id="target-echo",
        material_id="echo-exp",
    )
    character = _character(
        "unknown",
        echoes=tuple(slots),
        thresholds=(
            FunctionalThreshold(
                "er",
                "共鳴效率",
                None,
                130,
                "%",
                cultivation_target_id="target-er",
            ),
        ),
        skill_tasks=(CultivationTaskProgress("intro", "變奏技能", None, 6),),
    )

    queue = build_breadth_first_queue((character,))

    assert all(action.phase is ProgressionPhase.CONFIRMATION for action in queue)
    assert all(action.requires_manual_confirmation for action in queue)
    assert {action.issue for action in queue} == {
        ProgressionIssue.ECHO_VALIDITY_UNKNOWN,
        ProgressionIssue.FUNCTIONAL_THRESHOLD_UNKNOWN,
        ProgressionIssue.SKILL_INCOMPLETE,
    }
    echo_action = next(
        action for action in queue if action.issue is ProgressionIssue.ECHO_VALIDITY_UNKNOWN
    )
    assert echo_action.character_id == "unknown"
    assert echo_action.cultivation_target_id == "target-echo"
    assert echo_action.material_id == "echo-exp"
    assert "尚未確認" in echo_action.reason


def test_confirmed_invalid_echo_precedes_unknown_confirmation_across_characters() -> None:
    unknown_slots = list(_character("unknown").echo_slots)
    unknown_slots[0] = _echo(1, set_valid=None)
    invalid_slots = list(_character("invalid").echo_slots)
    invalid_slots[0] = _echo(1, set_valid=False)

    queue = build_breadth_first_queue(
        (
            _character("unknown", priority=0, echoes=tuple(unknown_slots)),
            _character("invalid", priority=999, echoes=tuple(invalid_slots)),
        )
    )

    assert queue[0].issue is ProgressionIssue.ECHO_SET_OR_MAIN_INVALID
    assert queue[-1].issue is ProgressionIssue.ECHO_VALIDITY_UNKNOWN


def test_one_confirmed_invalid_field_wins_over_another_unknown_field() -> None:
    slots = list(_character("mixed").echo_slots)
    slots[0] = _echo(1, set_valid=False, main_valid=None)

    queue = build_breadth_first_queue((_character("mixed", echoes=tuple(slots)),))

    assert queue[0].issue is ProgressionIssue.ECHO_SET_OR_MAIN_INVALID
    assert not queue[0].requires_manual_confirmation


def test_only_lowest_scoring_valid_echo_is_queued_for_improvement() -> None:
    slots = (
        _echo(1, score=60),
        _echo(2, score=40),
        _echo(3, score=40),
        _echo(4, score=80),
        _echo(5, score=90),
    )

    queue = build_breadth_first_queue((_character("alpha", echoes=slots),))

    weak = [action for action in queue if action.issue is ProgressionIssue.WEAKEST_ECHO]
    assert len(weak) == 1
    assert weak[0].target_key == "echo-slot-2"
    assert "40" in weak[0].reason


def test_goal_reached_characters_are_lower_within_the_same_phase() -> None:
    low_priority_slots = list(_character("unfinished").echo_slots)
    low_priority_slots[0] = _echo(1, score=50)
    high_priority_slots = list(_character("qualified").echo_slots)
    high_priority_slots[0] = _echo(1, score=50)

    queue = build_breadth_first_queue(
        (
            _character(
                "qualified",
                priority=0,
                reached=True,
                echoes=tuple(high_priority_slots),
            ),
            _character(
                "unfinished",
                priority=999,
                reached=False,
                echoes=tuple(low_priority_slots),
            ),
        )
    )

    assert [action.character_id for action in queue] == ["unfinished", "qualified"]


def test_queue_is_input_order_independent_and_excludes_non_candidates() -> None:
    first = _character(
        "b",
        skill_tasks=(CultivationTaskProgress("core", "核心技能", 8, 10),),
    )
    second = _character(
        "a",
        skill_tasks=(CultivationTaskProgress("core", "核心技能", 8, 10),),
    )
    archived = _character(
        "archived",
        candidate=False,
        skill_tasks=(CultivationTaskProgress("core", "核心技能", 1, 10),),
    )

    forward = build_breadth_first_queue((first, second, archived))
    reverse = build_breadth_first_queue((archived, second, first))

    assert [action.action_id for action in forward] == [action.action_id for action in reverse]
    assert [action.character_id for action in forward] == ["a", "b"]


def test_at_most_threshold_is_supported() -> None:
    threshold = FunctionalThreshold(
        "rotation",
        "循環時間",
        24,
        20,
        "秒",
        ThresholdDirection.AT_MOST,
    )

    queue = build_breadth_first_queue((_character("alpha", thresholds=(threshold,)),))

    assert queue[0].issue is ProgressionIssue.FUNCTIONAL_THRESHOLD_UNMET
    assert "至多" in queue[0].reason


def test_generic_tasks_cover_character_rank_and_small_node_progress() -> None:
    character = _character(
        "alpha",
        cultivation_tasks=(
            CultivationTaskProgress(
                "character-level",
                "角色等級",
                80,
                90,
                cultivation_target_id="target-level",
                material_id="character-exp",
            ),
            CultivationTaskProgress("weapon-rank", "武器諧振階級", 1, 5),
            CultivationTaskProgress("small-nodes", "小型屬性節點", 8, 10),
        ),
    )

    queue = build_breadth_first_queue((character,))

    assert len(queue) == 3
    assert all(action.phase is ProgressionPhase.SKILL_OR_WEAPON for action in queue)
    assert all(action.issue is ProgressionIssue.CULTIVATION_TASK_INCOMPLETE for action in queue)
    assert {action.target_key for action in queue} == {
        "cultivation-character-level",
        "cultivation-weapon-rank",
        "cultivation-small-nodes",
    }
    level_action = next(action for action in queue if "character-level" in action.target_key)
    assert level_action.cultivation_target_id == "target-level"
    assert level_action.material_id == "character-exp"


def test_ambiguous_character_inputs_fail_fast() -> None:
    with pytest.raises(ValueError, match="character ids must be unique"):
        build_breadth_first_queue((_character("same"), _character("SAME")))
    with pytest.raises(ValueError, match="slot indices must be unique"):
        _character("bad", echoes=(_echo(1), _echo(1)))
    with pytest.raises(ValueError, match="must not contain assessment data"):
        PlanningEchoSlot(1, False, score=10)
