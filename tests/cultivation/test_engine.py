from __future__ import annotations

import pytest

from wuwa_assistant.cultivation import (
    BlockerCode,
    CharacterCultivationInput,
    CultivationRules,
    CultivationSection,
    EchoSlotProgress,
    EchoSlotStatus,
    ProgressTarget,
    SectionWeights,
    SkillPriority,
    SkillProgress,
    WeaponProgress,
    evaluate_cultivation,
)


def _weapon(
    *,
    level: int = 90,
    target_level: int = 90,
    rank: int = 1,
    target_rank: int = 1,
) -> WeaponProgress:
    return WeaponProgress(
        "示範武器",
        level=ProgressTarget(level, target_level),
        rank=ProgressTarget(rank, target_rank),
    )


def _skills(*, core: int = 10, secondary: int = 6) -> tuple[SkillProgress, ...]:
    return (
        SkillProgress("核心技能", core, SkillPriority.CORE),
        SkillProgress("次要技能", secondary, SkillPriority.SECONDARY),
    )


def _echo(
    index: int,
    *,
    score: float = 70,
    set_is_valid: bool | None = True,
    main_stat_is_valid: bool | None = True,
) -> EchoSlotProgress:
    return EchoSlotProgress(
        index,
        is_filled=True,
        set_is_valid=set_is_valid,
        main_stat_is_valid=main_stat_is_valid,
        score=score,
        echo_id=f"echo-{index}",
    )


def _complete_input(**overrides: object) -> CharacterCultivationInput:
    values: dict[str, object] = {
        "character_name": "示範角色",
        "level": ProgressTarget(90, 90),
        "weapon": _weapon(),
        "skills": _skills(),
        "small_nodes": (True,) * 10,
        "echo_slots": tuple(_echo(index) for index in range(1, 6)),
    }
    values.update(overrides)
    return CharacterCultivationInput(**values)  # type: ignore[arg-type]


def _codes(result: object) -> tuple[BlockerCode, ...]:
    assert hasattr(result, "blockers")
    return tuple(blocker.code for blocker in result.blockers)  # type: ignore[attr-defined]


def test_default_targets_are_complete_at_inclusive_boundaries() -> None:
    result = evaluate_cultivation(_complete_input())

    assert result.overall_percent == 100.0
    assert result.at_goal
    assert result.blockers == ()
    assert result.echo_passing_count == 5
    assert result.echo_required_count == 5
    assert result.weakest_echo_slot == 1
    assert all(section.percent == 100 for section in result.sections)
    assert result.section(CultivationSection.SKILLS).components[0].target == 10
    assert result.section(CultivationSection.SKILLS).components[1].target == 6


def test_section_and_overall_percentages_are_transparent_and_capped() -> None:
    result = evaluate_cultivation(
        _complete_input(
            level=ProgressTarget(45, 90),
            weapon=_weapon(level=45, rank=1, target_rank=5),
            skills=_skills(core=5, secondary=3),
            small_nodes=(True,) * 5 + (False,) * 5,
            echo_slots=tuple(_echo(index, score=35) for index in range(1, 6)),
        )
    )

    assert result.section(CultivationSection.CHARACTER).percent == 50.0
    assert result.section(CultivationSection.WEAPON).percent == 35.0
    assert result.section(CultivationSection.SKILLS).percent == 50.0
    assert result.section(CultivationSection.SMALL_NODES).percent == 50.0
    assert result.section(CultivationSection.ECHOES).percent == 50.0
    assert result.overall_percent == 47.0
    assert not result.at_goal
    assert result.echo_passing_count == 0

    over_target = evaluate_cultivation(
        _complete_input(level=ProgressTarget(100, 90), weapon=_weapon(level=100))
    )
    assert over_target.section(CultivationSection.CHARACTER).percent == 100.0
    assert over_target.section(CultivationSection.WEAPON).percent == 100.0


def test_missing_echo_slots_are_materialized_and_block_goal() -> None:
    result = evaluate_cultivation(_complete_input(echo_slots=(_echo(1),)))

    assert len(result.echo_slots) == 5
    assert result.echo_slots[0].status is EchoSlotStatus.PASSING
    assert all(slot.status is EchoSlotStatus.MISSING for slot in result.echo_slots[1:])
    assert result.echo_passing_count == 1
    assert result.weakest_echo_slot == 2
    assert _codes(result).count(BlockerCode.ECHO_MISSING) == 4
    assert not result.at_goal


def test_invalid_set_and_main_stat_never_receive_score_credit() -> None:
    slots = list(_complete_input().echo_slots)
    slots[2] = _echo(3, score=100, set_is_valid=False, main_stat_is_valid=False)

    result = evaluate_cultivation(_complete_input(echo_slots=tuple(slots)))
    assessment = result.echo_slots[2]

    assert assessment.status is EchoSlotStatus.INVALID
    assert assessment.readiness_percent == 0.0
    assert not assessment.is_passing
    assert assessment.issues == (
        BlockerCode.ECHO_SET_INVALID,
        BlockerCode.ECHO_MAIN_STAT_INVALID,
    )
    assert BlockerCode.ECHO_SET_INVALID in _codes(result)
    assert BlockerCode.ECHO_MAIN_STAT_INVALID in _codes(result)


def test_unknown_validity_and_missing_score_require_confirmation() -> None:
    slots = list(_complete_input().echo_slots)
    slots[0] = EchoSlotProgress(
        1,
        is_filled=True,
        set_is_valid=None,
        main_stat_is_valid=None,
        score=None,
        echo_id="unconfirmed",
    )

    result = evaluate_cultivation(_complete_input(echo_slots=tuple(slots)))
    assessment = result.echo_slots[0]

    assert assessment.status is EchoSlotStatus.NEEDS_CONFIRMATION
    assert assessment.readiness_percent == 0
    assert assessment.issues == (
        BlockerCode.ECHO_SET_UNKNOWN,
        BlockerCode.ECHO_MAIN_STAT_UNKNOWN,
        BlockerCode.ECHO_SCORE_MISSING,
    )
    assert not result.at_goal


def test_below_threshold_and_weakest_slot_are_deterministic() -> None:
    result = evaluate_cultivation(
        _complete_input(
            echo_slots=(
                _echo(5, score=90),
                _echo(3, score=69),
                _echo(1, score=69),
                _echo(4, score=80),
                _echo(2, score=80),
            )
        )
    )

    assert result.echo_slots[0].slot_index == 1
    assert result.echo_slots[0].status is EchoSlotStatus.BELOW_THRESHOLD
    assert result.echo_slots[0].readiness_percent == pytest.approx(98.57)
    assert result.weakest_echo_slot == 1
    assert result.echo_passing_count == 3
    assert _codes(result).count(BlockerCode.ECHO_SCORE_BELOW_THRESHOLD) == 2


def test_custom_rules_targets_threshold_and_weights_are_honored() -> None:
    rules = CultivationRules(
        core_skill_target=9,
        secondary_skill_target=8,
        required_small_nodes=2,
        required_echo_slots=2,
        echo_passing_score=80,
        section_weights=SectionWeights(
            character=0,
            weapon=0,
            skills=1,
            small_nodes=1,
            echoes=2,
        ),
    )
    progress = CharacterCultivationInput(
        character_name="自訂角色",
        level=ProgressTarget(1, 90),
        weapon=None,
        skills=(
            SkillProgress("核心", 9, SkillPriority.CORE),
            SkillProgress("次要", 7, SkillPriority.SECONDARY, target_level=7),
        ),
        small_nodes=(True, True),
        echo_slots=(_echo(1, score=80), _echo(2, score=40)),
    )

    result = evaluate_cultivation(progress, rules)

    assert result.section(CultivationSection.SKILLS).percent == 100
    assert result.section(CultivationSection.SMALL_NODES).percent == 100
    assert result.section(CultivationSection.ECHOES).percent == 75
    assert result.overall_percent == 87.5
    assert result.echo_passing_count == 1
    # Zero-weight sections do not inflate the percentage, but remain real blockers.
    assert not result.at_goal
    assert BlockerCode.CHARACTER_LEVEL in _codes(result)
    assert BlockerCode.WEAPON_MISSING in _codes(result)


def test_missing_weapon_skills_and_node_states_report_explicit_blockers() -> None:
    result = evaluate_cultivation(
        _complete_input(weapon=None, skills=(), small_nodes=(True, False))
    )

    assert result.section(CultivationSection.WEAPON).percent == 0
    assert result.section(CultivationSection.SKILLS).percent == 0
    assert _codes(result).count(BlockerCode.WEAPON_MISSING) == 1
    assert _codes(result).count(BlockerCode.SKILLS_MISSING) == 1
    assert _codes(result).count(BlockerCode.SMALL_NODE_INCOMPLETE) == 1
    assert _codes(result).count(BlockerCode.SMALL_NODE_UNKNOWN) == 8


@pytest.mark.parametrize(
    ("constructor", "message"),
    [
        (lambda: ProgressTarget(-1, 90), "non-negative"),
        (lambda: ProgressTarget(1, 0), "positive"),
        (
            lambda: EchoSlotProgress(1, True, True, True, 101),
            "between zero and one hundred",
        ),
        (
            lambda: EchoSlotProgress(1, False, score=70),
            "must not contain echo data",
        ),
        (lambda: CultivationRules(required_echo_slots=0), "must be positive"),
        (lambda: SectionWeights(0, 0, 0, 0, 0), "at least one"),
        (lambda: SectionWeights(echoes=float("inf")), "must be finite"),
    ],
)
def test_input_boundaries_fail_fast(constructor: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        constructor()  # type: ignore[operator]


def test_collection_ambiguities_fail_fast() -> None:
    with pytest.raises(ValueError, match="skill names must be unique"):
        evaluate_cultivation(
            _complete_input(
                skills=(
                    SkillProgress("Core", 10, SkillPriority.CORE),
                    SkillProgress(" core ", 10, SkillPriority.CORE),
                )
            )
        )
    with pytest.raises(ValueError, match="slot indices must be unique"):
        evaluate_cultivation(_complete_input(echo_slots=(_echo(1), _echo(1))))
    with pytest.raises(ValueError, match="exceeds configured"):
        evaluate_cultivation(_complete_input(echo_slots=(_echo(6),)))
    with pytest.raises(ValueError, match="node state count exceeds"):
        evaluate_cultivation(_complete_input(small_nodes=(True,) * 11))
    with pytest.raises(TypeError, match="bool or None"):
        _complete_input(small_nodes=(True, 1))


def test_unknown_section_lookup_raises_key_error() -> None:
    result = evaluate_cultivation(_complete_input())
    with pytest.raises(KeyError):
        result.section("not-a-section")  # type: ignore[arg-type]
