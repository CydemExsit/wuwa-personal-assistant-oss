from __future__ import annotations

from datetime import UTC, datetime

from wuwa_assistant.application.character_completion import evaluate_character_detail
from wuwa_assistant.application.characters import (
    CharacterDetail,
    CharacterSummary,
    EchoSlotSnapshot,
    SkillSnapshot,
    WeaponSnapshot,
)


def _detail(*, score: float | None = 80, set_name: str | None = "凝夜白霜") -> CharacterDetail:
    now = datetime.now(UTC).isoformat()
    summary = CharacterSummary(
        id="character-1",
        data_set_id="data-set-1",
        name="測試角色",
        element="冷凝",
        role="輸出",
        is_candidate=True,
        priority=1,
        level=90,
        resonance_chain=0,
        scoring_profile_id=None,
        scoring_profile_name=None,
        completed_skill_count=5,
        skill_count=5,
        completed_small_stat_nodes=10,
        equipped_echo_count=5,
        archived_at=None,
    )
    skills = tuple(
        SkillSnapshot(
            id=f"skill-{index}",
            skill_key=f"skill-{index}",
            name=f"技能 {index}",
            skill_type="core" if index == 1 else "secondary",
            current_level=10 if index == 1 else 6,
            target_level=10 if index == 1 else 6,
            is_core=index == 1,
            is_completed=True,
            display_order=index,
        )
        for index in range(1, 6)
    )
    slots = tuple(
        EchoSlotSnapshot(
            slot_index=index,
            echo_id=f"echo-{index}",
            name="聲骸",
            set_name=set_name,
            cost=1,
            level=25,
            score=score,
            final_status="equipped",
            main_stats=({"name": "暴擊"},),
            sub_stats=(),
            score_profile_id="profile-dps",
            score_profile_version=3,
        )
        for index in range(1, 6)
    )
    return CharacterDetail(
        summary=summary,
        cultivation_goal={
            "echo_slot_confirmations": {
                str(index): {
                    "echo_id": f"echo-{index}",
                    "set": True,
                    "main_stat": True,
                }
                for index in range(1, 6)
            }
        },
        small_stat_nodes=(True,) * 10,
        notes=None,
        weapon=WeaponSnapshot("weapon-1", "武器", None, 90, 1, None),
        skills=skills,
        echo_slots=slots,
        teams=(),
        created_at=now,
        updated_at=now,
    )


def test_adapter_reports_complete_confirmed_character() -> None:
    result = evaluate_character_detail(_detail())

    assert result.completion.at_goal
    assert result.completion.overall_percent == 100
    assert result.completion.echo_passing_count == 5
    assert result.next_action == "已達目前養成目標"


def test_adapter_rejects_explicit_set_target_mismatch() -> None:
    detail = _detail()
    detail = CharacterDetail(
        summary=detail.summary,
        cultivation_goal={"echo_set_targets": {"1": ["浮星祛暗"]}},
        small_stat_nodes=detail.small_stat_nodes,
        notes=detail.notes,
        weapon=detail.weapon,
        skills=detail.skills,
        echo_slots=detail.echo_slots,
        teams=detail.teams,
        created_at=detail.created_at,
        updated_at=detail.updated_at,
    )

    result = evaluate_character_detail(detail)

    assert not result.completion.echo_slots[0].is_passing
    assert result.next_action == "優先改善永久聲骸位置 1"


def test_adapter_keeps_unknown_echo_data_unconfirmed() -> None:
    result = evaluate_character_detail(_detail(score=None, set_name=None))

    assert result.completion.echo_passing_count == 0
    assert not result.completion.at_goal
    assert result.next_action == "確認永久聲骸位置 1 的資料"


def test_adapter_rejects_score_from_another_profile_or_version() -> None:
    result = evaluate_character_detail(
        _detail(),
        scoring_profile_id="profile-healing",
        scoring_profile_version=3,
    )
    stale = evaluate_character_detail(
        _detail(),
        scoring_profile_id="profile-dps",
        scoring_profile_version=4,
    )

    assert result.completion.echo_passing_count == 0
    assert stale.completion.echo_passing_count == 0
    assert result.next_action == "確認永久聲骸位置 1 的資料"


def test_adapter_requires_target_or_explicit_confirmation_for_validity() -> None:
    detail = _detail()
    unconfirmed = CharacterDetail(
        summary=detail.summary,
        cultivation_goal={},
        small_stat_nodes=detail.small_stat_nodes,
        notes=detail.notes,
        weapon=detail.weapon,
        skills=detail.skills,
        echo_slots=detail.echo_slots,
        teams=detail.teams,
        created_at=detail.created_at,
        updated_at=detail.updated_at,
    )

    result = evaluate_character_detail(unconfirmed)

    assert result.completion.echo_passing_count == 0
    assert result.completion.echo_slots[0].status.value == "needs_confirmation"


def test_confirmation_does_not_transfer_to_a_replacement_echo() -> None:
    detail = _detail()
    first = detail.echo_slots[0]
    replaced_slot = EchoSlotSnapshot(
        slot_index=first.slot_index,
        echo_id="replacement-echo",
        name=first.name,
        set_name=first.set_name,
        cost=first.cost,
        level=first.level,
        score=first.score,
        final_status=first.final_status,
        main_stats=first.main_stats,
        sub_stats=first.sub_stats,
        score_profile_id=first.score_profile_id,
        score_profile_version=first.score_profile_version,
        score_observation_id=first.score_observation_id,
    )
    replaced = CharacterDetail(
        summary=detail.summary,
        cultivation_goal=detail.cultivation_goal,
        small_stat_nodes=detail.small_stat_nodes,
        notes=detail.notes,
        weapon=detail.weapon,
        skills=detail.skills,
        echo_slots=(replaced_slot, *detail.echo_slots[1:]),
        teams=detail.teams,
        created_at=detail.created_at,
        updated_at=detail.updated_at,
    )

    result = evaluate_character_detail(replaced)

    assert not result.completion.echo_slots[0].is_passing
    assert result.completion.echo_slots[0].status.value == "needs_confirmation"
