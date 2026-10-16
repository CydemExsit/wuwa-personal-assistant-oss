from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.application.equipment_overview import EquipmentOverviewService
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.application.scoring_workflow import ScoringWorkflowService
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    DataSetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    ScoringProfileRecord,
    StatWeightRecord,
    utc_now,
)


@dataclass
class Fixture:
    database: Database
    data_set_id: str
    capture_id: str
    profiles: ScoringProfileService
    profile_id: str
    service: EquipmentOverviewService

    def character(self, name: str = "角色") -> str:
        return (
            CharacterCultivationService(self.database.session_factory)
            .create_character(data_set_id=self.data_set_id, command=CreateCharacter(name=name))
            .summary.id
        )


@pytest.fixture
def equipment(tmp_path: Path) -> Iterator[Fixture]:
    database = create_database(AppPaths.from_root(tmp_path / "overview"))
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        capture = CaptureSessionRecord(
            data_set_id=data_set.id, backend_kind="fake", monitor_index=0, app_version="1.0.8"
        )
        session.add(capture)
        session.flush()
        data_set_id, capture_id = data_set.id, capture.id
    profiles = ScoringProfileService(database.session_factory)
    seeded = profiles.ensure_system_templates(load_template_seed_definitions())
    generic = next(row for row in seeded if row.template_kind == "generic_dps")
    try:
        yield Fixture(
            database,
            data_set_id,
            capture_id,
            profiles,
            generic.id,
            EquipmentOverviewService(database.session_factory),
        )
    finally:
        database.dispose()


def _stats() -> list[dict[str, Any]]:
    return [
        {"name": name, "value": value, "unit": "%", "confidence": 0.98, "slot_index": index}
        for index, (name, value) in enumerate(
            [
                ("暴擊", 7.5),
                ("暴擊傷害", 15.0),
                ("攻擊", 10.1),
                ("共鳴效率", 10.0),
                ("普攻傷害加成", 7.9),
            ],
            start=1,
        )
    ]


def _echo(
    fixture: Fixture,
    owner: str,
    slot: int | None,
    *,
    name: str | None = None,
    level: int = 25,
    stats: list[dict[str, Any]] | None = None,
    review: bool = False,
    consumed: bool = False,
    archived: bool = False,
    observation: bool = True,
) -> tuple[str, str | None]:
    values = _stats()[: level // 5] if stats is None else stats
    payload = {
        "name": name or f"聲骸{slot}",
        "level": level,
        "cost": 1,
        "main_stats": [
            {"name": "攻擊", "value": 18.0, "unit": "%", "stat_kind": "primary"},
            {"name": "攻擊", "value": 150, "unit": "", "stat_kind": "fixed"},
        ],
        "sub_stats": values,
    }
    with fixture.database.session_factory.begin() as session:
        echo = EchoRecord(
            data_set_id=fixture.data_set_id,
            character_id=owner,
            slot_index=slot,
            name=payload["name"],
            level=level,
            cost=1,
            set_name="套裝",
            main_stats_json=payload["main_stats"],
            sub_stats_json=values,
            final_status="equipped",
            score=99.0,
            archived_at=utc_now() if archived else None,
        )
        session.add(echo)
        session.flush()
        lineage = EchoSessionRecord(
            capture_session_id=fixture.capture_id,
            echo_id=echo.id,
            local_session_id=echo.id,
            was_consumed=consumed,
        )
        session.add(lineage)
        session.flush()
        if not observation:
            return echo.id, None
        row = EchoObservationRecord(
            echo_session_id=lineage.id,
            sequence_no=1,
            page_type="echo_selection",
            level=level,
            name=payload["name"],
            cost=1,
            main_stats_json=payload["main_stats"],
            sub_stats_json=values,
            raw_payload_json=deepcopy(payload),
            effective_payload_json=deepcopy(payload),
            requires_review=review,
        )
        session.add(row)
        session.flush()
        return echo.id, row.id


def _dump(database: Database) -> str:
    with sqlite3.connect(database.paths.database_path) as connection:
        return "\n".join(connection.iterdump())


def test_auto_character_profiles_match_live_scoring_and_preserve_database(
    equipment: Fixture,
) -> None:
    from wuwa_assistant.scoring.character_guides import (
        AUTO_PROFILE_ID,
        load_character_seed_definitions,
    )

    equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    owner = equipment.character("莫寧")
    _, observation_id = _echo(equipment, owner, 1)
    unknown = equipment.character("沒有攻略")
    _echo(equipment, unknown, 1)
    before = _dump(equipment.database)
    overview = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    )
    row = next(r for r in overview.characters if r.character_id == owner)
    assert row.template_kind == "character_guide_1209"
    assert row.slots[0].score is not None and "建議共鳴效率 260%" in row.scoring_notes[0]
    assert next(r for r in overview.characters if r.character_id == unknown).scored_count == 0
    with equipment.database.session_factory() as session:
        observation = session.get(EchoObservationRecord, observation_id)
        assert observation is not None
        result = ScoringWorkflowService(
            equipment.database.session_factory, equipment.profiles
        ).evaluate(
            observation.effective_payload_json,
            data_set_id=equipment.data_set_id,
            echo_session_id=observation.echo_session_id,
            observation_id=observation.id,
            character_mode=True,
            persist=False,
        )
    assert result.profile_id == row.slots[0].profile_id
    assert result.breakdown.score == row.slots[0].score
    assert result.character_label == "莫寧"
    assert _dump(equipment.database) == before


def test_explicit_custom_profile_wins_over_guide_and_is_not_adapted(equipment: Fixture) -> None:
    from wuwa_assistant.scoring.character_guides import (
        AUTO_PROFILE_ID,
        load_character_seed_definitions,
    )

    seeded = equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    source = next(p for p in seeded if p.template_kind == "character_guide_1209")
    custom = equipment.profiles.clone_template(
        source.id, data_set_id=equipment.data_set_id, name="依遊戲內推薦自訂"
    )
    owner = equipment.character("莫寧")
    _echo(equipment, owner, 1)
    with equipment.database.session_factory.begin() as session:
        character = session.get(CharacterRecord, owner)
        assert character is not None
        character.scoring_profile_id = custom.id
        character.cultivation_goal_json = {"scoring_panel_energy_regen": 300}
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    assert row.slots[0].profile_id == custom.id
    assert row.profile_name == "依遊戲內推薦自訂"
    assert "共鳴效率 1" in row.weight_summary and not row.scoring_notes


def test_character_context_persistence_is_idempotent_and_rejects_stale_owner(
    equipment: Fixture,
) -> None:
    from wuwa_assistant.scoring import EchoScoringEngine
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions
    from wuwa_assistant.storage.orm import UpgradeDecisionRecord

    equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    owner = equipment.character("達妮婭")
    _, observation_id = _echo(equipment, owner, 1)
    with equipment.database.session_factory.begin() as session:
        character = session.get(CharacterRecord, owner)
        character.cultivation_goal_json = {"scoring_panel_energy_regen": 120}
        observation = session.get(EchoObservationRecord, observation_id)
        payload, lineage = deepcopy(observation.effective_payload_json), observation.echo_session_id
    workflow = ScoringWorkflowService(equipment.database.session_factory, equipment.profiles)
    kwargs = dict(
        data_set_id=equipment.data_set_id,
        echo_session_id=lineage,
        observation_id=observation_id,
        character_mode=True,
    )
    first = workflow.evaluate(payload, **kwargs)
    again = workflow.evaluate(payload, **kwargs)
    assert first.persisted_decision_id == again.persisted_decision_id
    assert any("降至 0.1" in note for note in first.breakdown.notes)
    with equipment.database.session_factory() as session:
        saved = session.get(UpgradeDecisionRecord, first.persisted_decision_id)
        assert saved.scoring_context_json["current_totals"]["energy_regen"] == 120

    class ChangingContextScorer(EchoScoringEngine):
        def score(self, *args, **kwargs):
            with equipment.database.session_factory.begin() as session:
                character = session.get(CharacterRecord, owner)
                character.cultivation_goal_json = {
                    "scoring_panel_energy_regen": 130,
                    "scoring_energy_target": 140,
                }
            return super().score(*args, **kwargs)

    changing = ScoringWorkflowService(
        equipment.database.session_factory,
        equipment.profiles,
        scoring_engine=ChangingContextScorer(),
    )
    with pytest.raises(RuntimeError, match="scoring inputs changed"):
        changing.evaluate(payload, **kwargs)
    with equipment.database.session_factory() as session:
        assert len(session.scalars(select(UpgradeDecisionRecord)).all()) == 1


def test_complete_scores_use_same_engine_and_profile_without_writing(equipment: Fixture) -> None:
    owner = equipment.character()
    ids = [_echo(equipment, owner, slot) for slot in range(1, 6)]
    before = _dump(equipment.database)
    overview = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    row = overview.characters[0]
    assert row.equipped_count == row.scored_count == 5
    assert row.total_score is not None and row.average_score == round(row.total_score / 5, 2)
    assert row.partial_average_score is None
    assert len(overview.weakest_echoes) == 5
    with equipment.database.session_factory() as session:
        observation = session.get(EchoObservationRecord, ids[0][1])
        assert observation is not None
        expected = ScoringWorkflowService(
            equipment.database.session_factory, equipment.profiles
        ).evaluate(
            observation.effective_payload_json, data_set_id=equipment.data_set_id, persist=False
        )
    assert row.slots[0].score == expected.breakdown.score
    assert row.slots[0].score != 99.0
    assert row.slots[0].observation_id == ids[0][1]
    assert row.slots[0].payload_version == 1
    assert all(piece.profile_id == overview.profile_id for piece in row.slots)
    assert all(piece.set_is_valid is None for piece in row.slots)
    assert _dump(equipment.database) == before


def test_partial_and_uncertain_values_never_count_as_zero(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1)
    _echo(equipment, owner, 2, stats=_stats()[:3])
    _echo(equipment, owner, 3, review=True)
    _echo(equipment, owner, 4, level=0)
    _echo(equipment, owner, None, name="未定位置")
    overview = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    row = overview.characters[0]
    assert row.scored_count == 1
    assert row.equipped_count == 4
    assert row.average_score is row.total_score is None
    assert row.partial_average_score == row.slots[0].score
    assert [piece.status for piece in row.slots] == [
        "scored",
        "incomplete",
        "needs_review",
        "unrolled",
        "missing",
    ]
    assert row.pending_echo_count == 1
    assert row.pending_echoes[0].slot_index is None
    assert len(overview.weakest_echoes) == 1


@pytest.mark.parametrize("level", [15, 17])
def test_lower_level_complete_observation_keeps_two_distinct_score_scales(
    equipment: Fixture,
    level: int,
) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, level=level)
    piece = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[0]
    assert piece.level == level
    assert piece.score is not None and piece.opened_slot_quality_score is not None
    assert piece.opened_slot_quality_score > piece.score


@pytest.mark.parametrize("level", [17, 22, 24])
def test_equipment_score_matches_node_with_same_tuned_stats(equipment: Fixture, level: int) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, level=level)
    _echo(equipment, owner, 2, level=level // 5 * 5)
    exact, node = (
        equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[:2]
    )
    assert exact.level == level
    assert exact.score is not None and exact.score == node.score
    assert exact.status == node.status == "scored"


@pytest.mark.parametrize("level", range(1, 5))
def test_untuned_levels_before_five_are_unrolled_in_equipment_overview(
    equipment: Fixture,
    level: int,
) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, level=level)
    piece = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[0]
    assert piece.level == level and piece.status == "unrolled"
    assert piece.score is None


def test_latest_review_observation_invalidates_saved_score_and_keeps_manual_correction(
    equipment: Fixture,
) -> None:
    owner = equipment.character()
    echo_id, observation_id = _echo(equipment, owner, 1)
    with equipment.database.session_factory.begin() as session:
        observation = session.get(EchoObservationRecord, observation_id)
        assert observation is not None
        corrected = deepcopy(observation.effective_payload_json)
        corrected["sub_stats"][0]["value"] = 10.5
        observation.effective_payload_json = corrected
        observation.payload_version = 2
        echo = session.get(EchoRecord, echo_id)
        assert echo is not None
        echo.score = 1
    first = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[0]
    assert first.payload_version == 2 and first.score is not None and first.score != 1
    with equipment.database.session_factory.begin() as session:
        observation = session.get(EchoObservationRecord, observation_id)
        assert observation is not None
        observation.requires_review = True
    second = (
        equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[0]
    )
    assert second.score is None and second.status == "needs_review"


def test_shared_profile_selection_recomputes_all_characters(equipment: Fixture) -> None:
    for name in ("甲", "乙"):
        owner = equipment.character(name)
        _echo(equipment, owner, 1)
    generic = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    alternate = next(
        profile
        for profile in equipment.profiles.list_profiles(data_set_id=equipment.data_set_id)
        if profile.id != equipment.profile_id
    )
    changed = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=alternate.id
    )
    assert {row.slots[0].profile_id for row in changed.characters} == {alternate.id}
    assert changed.characters[0].slots[0].score != generic.characters[0].slots[0].score
    assert changed.characters[0].slots[0].score == changed.characters[1].slots[0].score


def test_archived_consumed_and_other_dataset_rows_are_excluded(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, consumed=True)
    _echo(equipment, owner, 2, archived=True)
    archived_owner = equipment.character("封存")
    _echo(equipment, archived_owner, 1)
    with equipment.database.session_factory.begin() as session:
        archived = session.get(CharacterRecord, archived_owner)
        assert archived is not None
        archived.archived_at = utc_now()
        other = DataSetRecord(name="其他", kind="demo")
        session.add(other)
        session.flush()
        session.add(CharacterRecord(data_set_id=other.id, name="不混入"))
    overview = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    assert len(overview.characters) == 1
    assert overview.characters[0].equipped_count == 0
    assert overview.weakest_echoes == ()


def test_pending_exact_rescan_is_grouped_with_manual_current_slot(equipment: Fixture) -> None:
    owner = equipment.character()
    echo_id, _ = _echo(equipment, owner, 1, name="相同聲骸")
    _echo(equipment, owner, None, name="相同聲骸")
    overview = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    assert overview.physical_echo_count == 2 and overview.logical_echo_count == 1
    assert overview.characters[0].slots[0].echo_id == echo_id
    assert overview.characters[0].slots[0].rescan_count == 1
    assert overview.characters[0].pending_echo_count == 0
    assert len(overview.weakest_echoes) == 1


def test_missing_observation_does_not_trust_saved_score(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, observation=False)
    piece = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[0]
    assert piece.score is None and piece.status == "missing_observation"


def test_incompatible_profile_version_is_explicitly_unscored(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1)
    with equipment.database.session_factory.begin() as session:
        profile = session.get(ScoringProfileRecord, equipment.profile_id)
        assert profile is not None
        profile.roll_data_version = "different-roll-data"
    piece = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0].slots[0]
    assert piece.score is None and piece.status == "profile_unavailable"


def test_empty_new_database_is_read_only_and_has_no_profile(tmp_path: Path) -> None:
    with create_database(AppPaths.from_root(tmp_path / "empty")) as database:
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="新的", kind="primary")
            session.add(data_set)
            session.flush()
            data_set_id = data_set.id
        service = EquipmentOverviewService(database.session_factory)
        before = _dump(database)
        empty = service.get_overview(data_set_id=data_set_id)
        assert empty.profile_id is None and empty.characters == ()
        assert _dump(database) == before
        CharacterCultivationService(database.session_factory).create_character(
            data_set_id=data_set_id, command=CreateCharacter(name="新角色")
        )
        row = service.get_overview(data_set_id=data_set_id).characters[0]
        assert row.scored_count == 0 and row.average_score is None
        assert {category.key for category in row.categories} == {
            "character",
            "weapon",
            "skills",
            "small_nodes",
            "echoes",
        }


def test_character_fit_overrides_remain_separate_from_shared_profile_score(
    equipment: Fixture,
) -> None:
    owner = equipment.character()
    echoes = [_echo(equipment, owner, slot) for slot in range(1, 6)]
    other = next(
        row
        for row in equipment.profiles.list_profiles(data_set_id=equipment.data_set_id)
        if row.id != equipment.profile_id
    )
    with equipment.database.session_factory.begin() as session:
        profile = session.get(ScoringProfileRecord, equipment.profile_id)
        assert profile is not None
        profile.usable_threshold = 5
        profile.passing_threshold = 10
        character = session.get(CharacterRecord, owner)
        assert character is not None
        character.scoring_profile_id = other.id
        character.cultivation_goal_json = {
            "echo_set_targets": {str(slot): "套裝" for slot in range(1, 6)},
            "echo_main_stat_targets": {str(slot): "攻擊" for slot in range(1, 6)},
            "echo_slot_confirmations": {
                "1": {"echo_id": echoes[0][0], "set": False, "main_stat": True},
                # A confirmation for a replaced Echo must not override its successor.
                "2": {"echo_id": "old-echo", "set": False, "main_stat": False},
            },
        }
    row = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0]
    assert row.scored_count == 5
    assert all(piece.profile_id == equipment.profile_id for piece in row.slots)
    assert row.slots[0].set_is_valid is False and row.slots[0].main_stat_is_valid is True
    assert row.slots[0].is_passing is False
    assert row.slots[1].set_is_valid is True and row.slots[1].main_stat_is_valid is True
    assert row.slots[1].is_passing is True
    assert row.slots[1].main_stats[0]["name"] == "攻擊"
    category = next(item for item in row.categories if item.key == "echoes")
    assert (category.done, category.target, category.at_goal) == (4, 5, False)


def test_a_reliably_scored_zero_is_ranked_while_unknown_is_excluded(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1)
    _echo(equipment, owner, 2, review=True)
    with equipment.database.session_factory.begin() as session:
        for weight in session.scalars(
            select(StatWeightRecord).where(
                StatWeightRecord.scoring_profile_id == equipment.profile_id,
                StatWeightRecord.stat_name.in_(
                    [
                        "crit_rate",
                        "crit_damage",
                        "attack_pct",
                        "energy_regen",
                        "basic_attack_damage",
                    ]
                ),
            )
        ):
            if weight.condition_key == "always":
                weight.weight = 0
            else:
                session.delete(weight)
    overview = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    row = overview.characters[0]
    assert row.scored_count == 1 and row.partial_average_score == 0
    assert row.slots[0].score == 0 and row.slots[1].score is None
    assert len(overview.weakest_echoes) == 1 and overview.weakest_echoes[0].score == 0


def test_identical_manual_slots_stay_distinct_in_character_overview(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, name="相同聲骸")
    _echo(equipment, owner, 2, name="相同聲骸")
    overview = equipment.service.get_overview(data_set_id=equipment.data_set_id)
    assert overview.logical_echo_count == 2
    assert overview.characters[0].equipped_count == 2
    assert len(overview.weakest_echoes) == 2


def _guide_equipment(equipment: Fixture, name: str = "洛瑟菈") -> str:
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions

    equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    owner = equipment.character(name)
    for slot in range(1, 6):
        echo_id, _ = _echo(equipment, owner, slot)
        with equipment.database.session_factory.begin() as session:
            row = session.get(EchoRecord, echo_id)
            assert row is not None
            row.set_name = "雪落無聲之願"
    return owner


def test_guide_detail_contains_exact_stat_contributions_without_writes(equipment: Fixture) -> None:
    from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

    _guide_equipment(equipment)
    before = _dump(equipment.database)
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    assert row.element == "冷凝" and row.role
    assert "5/5" in row.set_summary
    for piece in row.slots:
        assert piece.set_is_valid and piece.main_stat_is_valid
        assert len(piece.sub_stats) == len(piece.stat_details) == 5
        assert sum(s.contribution or 0 for s in piece.stat_details) == pytest.approx(piece.score)
        assert any(s.contribution == 0 and s.relevance == "不加分" for s in piece.stat_details)
    assert _dump(equipment.database) == before


def test_mixed_alternative_sets_are_not_a_complete_five_piece_build(equipment: Fixture) -> None:
    from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

    owner = _guide_equipment(equipment)
    with equipment.database.session_factory.begin() as session:
        for row in session.scalars(select(EchoRecord).where(EchoRecord.character_id == owner)):
            if row.slot_index in {1, 2}:
                row.set_name = "輕雲出月"
        for profile in session.scalars(select(ScoringProfileRecord)):
            profile.usable_threshold = 1
            profile.passing_threshold = 2
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    assert all(p.set_is_valid and p.is_passing for p in row.slots)
    category = next(c for c in row.categories if c.key == "echoes")
    assert not category.at_goal and category.done < category.target
    assert "尚未湊齊" in row.set_summary


def _mixed_guide_equipment(
    equipment: Fixture, sets: tuple[str | None, ...], *, pending_last: bool = False
) -> str:
    owner = _guide_equipment(equipment, "千咲")
    with equipment.database.session_factory.begin() as session:
        for piece in session.scalars(select(EchoRecord).where(EchoRecord.character_id == owner)):
            assert piece.slot_index is not None
            piece.set_name = sets[piece.slot_index - 1]
            if pending_last and piece.slot_index == 5:
                piece.slot_index = None
        for profile in session.scalars(select(ScoringProfileRecord)):
            profile.usable_threshold = 1
            profile.passing_threshold = 2
    return owner


@pytest.mark.parametrize(
    "sets,complete",
    [
        (("命理崩毀之弦",) * 3 + ("沉日劫明",) * 2, True),
        (("命理崩毀之弦",) * 3 + ("幽夜隱跡",) * 2, True),
        (("命理崩毀之弦",) * 2 + ("命理崩毁之弦",) + ("沉日劫明",) * 2, True),
        (("命理崩毀之弦",) * 5, False),
        (("命理崩毀之弦",) * 3 + ("沉日劫明", "幽夜隱跡"), False),
        (("命理崩毀之弦",) * 2 + ("沉日劫明",) * 3, False),
        (("命理崩毀之弦",) * 3 + ("沉日劫明", None), False),
    ],
)
def test_mixed_guide_requires_one_complete_three_plus_two_combination(
    equipment: Fixture, sets: tuple[str | None, ...], complete: bool
) -> None:
    from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

    _mixed_guide_equipment(equipment, sets)
    before = _dump(equipment.database)
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    category = next(c for c in row.categories if c.key == "echoes")
    assert category.target == 6
    assert category.at_goal is complete
    assert (category.done == category.target) is complete
    assert ("尚未湊齊參考混搭" not in row.set_summary) is complete
    assert "五件套" not in row.set_summary and "/5" not in row.set_summary
    if not complete:
        assert "命理崩毀之弦 3件" in row.set_summary
        assert "沉日劫明 2件" in row.set_summary
    if complete:
        assert "命理崩毀之弦 3件" in row.set_summary
        assert "命理崩毁" not in row.set_summary
    assert _dump(equipment.database) == before


def test_pending_piece_cannot_complete_mixed_guide_set(equipment: Fixture) -> None:
    from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

    _mixed_guide_equipment(equipment, ("命理崩毀之弦",) * 3 + ("沉日劫明",) * 2, pending_last=True)
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    category = next(c for c in row.categories if c.key == "echoes")
    assert row.equipped_count == 4 and row.pending_echo_count == 1
    assert not category.at_goal and category.done < category.target
    assert "沉日劫明 1件" in row.set_summary
    assert "尚未湊齊參考混搭" in row.set_summary


@pytest.mark.parametrize("name,element_set", [("露西", "浮星祛暗"), ("蕾貝卡", "徹空冥雷")])
@pytest.mark.parametrize("single_set", ["碎夢亡鬼之魘", "碎夢亡鬼之魔", None, "碎夢亡鬼之魂"])
def test_one_plus_two_plus_two_overview_accepts_only_known_set_names_without_writes(
    equipment: Fixture, name: str, element_set: str, single_set: str | None
) -> None:
    from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

    owner = _guide_equipment(equipment, name)
    actual_sets = (single_set, element_set, element_set, "不絕餘音", "不絕餘音")
    with equipment.database.session_factory.begin() as session:
        for piece in session.scalars(select(EchoRecord).where(EchoRecord.character_id == owner)):
            assert piece.slot_index is not None
            piece.set_name = actual_sets[piece.slot_index - 1]
        for profile in session.scalars(select(ScoringProfileRecord)):
            profile.usable_threshold = 1
            profile.passing_threshold = 2
    before = _dump(equipment.database)
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    recognized = single_set in {"碎夢亡鬼之魘", "碎夢亡鬼之魔"}
    assert row.equipped_count == 5
    assert ("尚未湊齊參考混搭" not in row.set_summary) is recognized
    assert row.slots[0].set_name == single_set  # Keep the original observation available.
    assert row.slots[0].set_is_valid is (None if single_set is None else recognized)
    if recognized:
        assert "碎夢亡鬼之魘 1件" in row.set_summary
        assert "碎夢亡鬼之魔" not in row.set_summary
    category = next(c for c in row.categories if c.key == "echoes")
    assert category.at_goal is recognized
    assert _dump(equipment.database) == before


@pytest.mark.parametrize("name,element_set", [("露西", "浮星祛暗"), ("蕾貝卡", "徹空冥雷")])
@pytest.mark.parametrize(
    "slot,candidate_set,breaks_set", [(1, "碎夢亡鬼之魘", False), (2, "不絕餘音", True)]
)
def test_legacy_one_piece_alias_keeps_mixed_set_replacement_guard(
    equipment: Fixture, name: str, element_set: str, slot: int, candidate_set: str, breaks_set: bool
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _guide_equipment(equipment, name)
    actual_sets = ("碎夢亡鬼之魔", element_set, element_set, "不絕餘音", "不絕餘音")
    with equipment.database.session_factory.begin() as session:
        for piece in session.scalars(select(EchoRecord).where(EchoRecord.character_id == owner)):
            assert piece.slot_index is not None
            piece.set_name = actual_sets[piece.slot_index - 1]
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, slot)
    stats = _stats()
    stats[0]["value"], stats[1]["value"] = 10.5, 21.0
    before = _dump(equipment.database)
    result = service.evaluate(
        {
            "level": 25,
            "cost": 1,
            "set_name": candidate_set,
            "main_stats": list(target.piece.main_stats),
            "sub_stats": stats,
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=slot,
    )
    if breaks_set:
        assert result.conclusion == "先檢查配裝"
        assert "現有套裝組合" in result.reason
        assert not result.target_comparable
    else:
        assert result.conclusion == "可考慮替換"
        assert result.target_comparable
    assert _dump(equipment.database) == before


@pytest.mark.parametrize("override_kind", ["targets", "confirmations"])
def test_explicit_custom_set_plan_takes_precedence_over_mixed_guide(
    equipment: Fixture, override_kind: str
) -> None:
    from wuwa_assistant.scoring.character_guides import AUTO_PROFILE_ID

    owner = _mixed_guide_equipment(equipment, ("命理崩毀之弦",) * 5)
    with equipment.database.session_factory.begin() as session:
        character = session.get(CharacterRecord, owner)
        assert character is not None
        if override_kind == "targets":
            character.cultivation_goal_json = {
                "echo_set_targets": {str(slot): "命理崩毀之弦" for slot in range(1, 6)}
            }
        else:
            character.cultivation_goal_json = {
                "echo_slot_confirmations": {
                    str(piece.slot_index): {"echo_id": piece.id, "set": True}
                    for piece in session.scalars(
                        select(EchoRecord).where(EchoRecord.character_id == owner)
                    )
                }
            }
    row = equipment.service.get_overview(
        data_set_id=equipment.data_set_id, profile_id=AUTO_PROFILE_ID
    ).characters[0]
    category = next(c for c in row.categories if c.key == "echoes")
    assert category.at_goal and category.done == category.target == 5
    assert "尚未湊齊" not in row.set_summary


@pytest.mark.parametrize(
    "slot,candidate_set,breaks_set",
    [(1, "沉日劫明", True), (4, "幽夜隱跡", True), (1, "命理崩毁之弦", False)],
)
def test_replacement_respects_complete_mixed_set_not_only_allowed_individual_names(
    equipment: Fixture, slot: int, candidate_set: str, breaks_set: bool
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _mixed_guide_equipment(equipment, ("命理崩毀之弦",) * 3 + ("沉日劫明",) * 2)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, slot)
    stats = _stats()
    stats[0]["value"] = 10.5
    stats[1]["value"] = 21.0
    before = _dump(equipment.database)
    result = service.evaluate(
        {
            "level": 25,
            "cost": 1,
            "set_name": candidate_set,
            "main_stats": list(target.piece.main_stats),
            "sub_stats": stats,
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=slot,
    )
    if breaks_set:
        assert result.conclusion == "先檢查配裝"
        assert "現有套裝組合" in result.reason
        assert not result.target_comparable
    else:
        assert result.conclusion == "可考慮替換"
        assert result.target_comparable
    assert _dump(equipment.database) == before


def test_incomplete_and_unfinished_stats_remain_visible(equipment: Fixture) -> None:
    owner = equipment.character()
    _echo(equipment, owner, 1, review=True)
    _echo(equipment, owner, 2, level=15)
    row = equipment.service.get_overview(data_set_id=equipment.data_set_id).characters[0]
    assert len(row.slots[0].stat_details) == 5
    assert row.slots[0].score is None
    assert row.slots[0].status == "needs_review"
    assert all(s.contribution is not None for s in row.slots[0].stat_details)
    assert row.slots[0].training_recommendation.action == "補齊辨識資料"
    assert row.slots[1].training_recommendation.action == "先看強化潛力"


@pytest.mark.parametrize(
    "set_name,cost,review,expected",
    [
        ("雪落無聲之願", 1, False, "保留現有聲骸"),
        ("輕雲出月", 1, False, "先檢查配裝"),
        ("雪落無聲之願", 3, False, "COST 不同"),
        (None, 1, False, "配裝資料待補"),
        ("雪落無聲之願", 1, True, "資料待補"),
    ],
)
def test_replacement_compares_fresh_role_baseline_and_never_assigns(
    equipment: Fixture,
    set_name: str | None,
    cost: int,
    review: bool,
    expected: str,
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    before = _dump(equipment.database)
    comparison = service.evaluate(
        {
            "level": 25,
            "cost": cost,
            "set_name": set_name,
            "main_stats": list(target.piece.main_stats),
            "sub_stats": _stats(),
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
        requires_review=review,
    )
    assert comparison.conclusion == expected
    assert comparison.target_comparable == (expected == "保留現有聲骸")
    if not comparison.target_comparable:
        assert comparison.delta is None
    assert comparison.evaluation.persisted_decision_id is None
    assert comparison.evaluation.breakdown.score == target.piece.score or review
    assert _dump(equipment.database) == before


def test_replacement_uses_updated_profile_and_panel_not_cached_target(equipment: Fixture) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    with equipment.database.session_factory.begin() as session:
        character = session.get(CharacterRecord, owner)
        character.scoring_profile_id = equipment.profile_id
        character.cultivation_goal_json = {"scoring_panel_energy_regen": 150}
    changed = service.target(equipment.data_set_id, owner, 1)
    assert changed.profile_id != target.profile_id
    assert changed.profile_id == equipment.profile_id
    assert changed.piece.profile_id == changed.profile_id


def test_manual_primary_target_cannot_match_fixed_secondary(equipment: Fixture) -> None:
    from wuwa_assistant.application.character_completion import _main_stat_validity
    from wuwa_assistant.application.characters import EchoSlotSnapshot
    from wuwa_assistant.application.equipment_analysis import primary_main_key

    slot = EchoSlotSnapshot(
        1,
        "e",
        "name",
        "set",
        1,
        25,
        40,
        "equipped",
        (
            {"name": "生命", "value": 22.8, "unit": "%", "stat_kind": "primary"},
            {"name": "攻擊", "value": 150, "unit": "", "stat_kind": "fixed"},
        ),
        (),
    )
    assert primary_main_key(slot.main_stats) == "hp_pct"
    assert _main_stat_validity(slot, {"echo_main_stat_targets": {"1": "攻擊"}}) is False
    assert primary_main_key((slot.main_stats[1],)) is None


@pytest.mark.parametrize("level", range(5))
def test_unrolled_replacement_has_a_first_step_not_a_fake_score(
    equipment: Fixture, level: int
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    result = service.evaluate(
        {
            "level": level,
            "cost": 1,
            "set_name": "雪落無聲之願",
            "main_stats": list(target.piece.main_stats),
            "sub_stats": [],
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
    )
    assert result.conclusion == "可先培養至 +15"
    assert f"+{level} → +15" in result.reason
    assert "調諧器 30 個" in result.reason
    assert not result.target_comparable and result.delta is None
    assert result.evaluation.breakdown.score == 0  # No earned substat points yet.
    assert result.evaluation.breakdown.opened_slot_quality_score is None
    assert result.evaluation.projection.improvement_probability is None
    assert result.evaluation.persisted_decision_id is None


def test_better_maxed_replacement_reports_delta(equipment: Fixture) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    stats = _stats()
    stats[0]["value"] = 10.5
    stats[1]["value"] = 21.0
    result = service.evaluate(
        {
            "level": 25,
            "cost": 1,
            "set_name": "雪落無聲之願",
            "main_stats": list(target.piece.main_stats),
            "sub_stats": stats,
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
    )
    assert result.conclusion == "可考慮替換" and result.delta > 0
    assert result.target_comparable


def test_unrolled_candidate_does_not_chase_a_perfect_target(
    equipment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    target = replace(target, piece=replace(target.piece, score=100))
    monkeypatch.setattr(service, "target", lambda *args: target)
    result = service.evaluate(
        {
            "level": 0,
            "cost": 1,
            "set_name": "雪落無聲之願",
            "main_stats": list(target.piece.main_stats),
            "sub_stats": [],
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
    )
    assert result.conclusion == "無法超過現有聲骸"
    assert not result.target_comparable


def test_maxed_candidate_does_not_discard_an_unfinished_current_piece(equipment: Fixture) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions

    equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    owner = equipment.character("洛瑟菈")
    echo_id, _ = _echo(equipment, owner, 1, level=15)
    with equipment.database.session_factory.begin() as session:
        row = session.get(EchoRecord, echo_id)
        row.set_name = "雪落無聲之願"
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    stats = _stats()
    stats[0]["value"] = 10.5
    stats[1]["value"] = 21
    result = service.evaluate(
        {
            "level": 25,
            "cost": 1,
            "set_name": "雪落無聲之願",
            "main_stats": list(target.piece.main_stats),
            "sub_stats": stats,
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
    )
    assert result.delta > 0
    assert result.conclusion == "現有聲骸尚有強化空間"


def test_beating_zero_score_gear_does_not_override_low_quality_stop(
    equipment: Fixture,
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions

    equipment.profiles.ensure_system_templates(load_character_seed_definitions())
    owner = equipment.character("洛瑟菈")
    irrelevant = [
        {"name": name, "value": value, "unit": "%", "confidence": 0.98, "slot_index": i}
        for i, (name, value) in enumerate(
            [
                ("共鳴效率", 10),
                ("重擊傷害加成", 7.9),
                ("共鳴技能傷害加成", 7.9),
                ("共鳴解放傷害加成", 7.9),
                ("防禦", 11.8),
            ],
            1,
        )
    ]
    echo_id, _ = _echo(equipment, owner, 1, stats=irrelevant)
    with equipment.database.session_factory.begin() as session:
        session.get(EchoRecord, echo_id).set_name = "雪落無聲之願"
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    assert target.piece.score == 0
    candidate_stats = deepcopy(irrelevant[:4])
    candidate_stats[0].update(name="暴擊", value=7.5)
    result = service.evaluate(
        {
            "level": 20,
            "cost": 1,
            "set_name": "雪落無聲之願",
            "main_stats": list(target.piece.main_stats),
            "sub_stats": candidate_stats,
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
    )
    assert result.evaluation.projection.maximum_score < 70
    assert result.delta > 0 and result.target_comparable
    assert result.conclusion == "已超過現有, 建議停止投入"
    assert "weak_baseline_quality" in " ".join(result.evaluation.decision.reason_codes)


@pytest.mark.parametrize("baseline", [10.0, 25.04])
@pytest.mark.parametrize("missing_quality_probability", [False, True])
def test_weak_replacement_quality_decision_reaches_application_without_writes(
    equipment: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    baseline: float,
    missing_quality_probability: bool,
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring import UpgradeAction

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 3)
    target = replace(target, piece=replace(target.piece, score=baseline, cost=3))
    monkeypatch.setattr(service, "target", lambda *args: target)
    if missing_quality_probability:
        enumerate_remaining = service._scoring._scorer.enumerate_remaining

        def missing_quality(*args, **kwargs):
            projection = enumerate_remaining(*args, **kwargs)
            return replace(
                projection,
                probabilities={k: v for k, v in projection.probabilities.items() if k != "usable"},
            )

        monkeypatch.setattr(service._scoring._scorer, "enumerate_remaining", missing_quality)
    before = _dump(equipment.database)
    result = service.evaluate(
        {
            "name": "風鱗蜃甲",
            "level": 22,
            "cost": 3,
            "set_name": "雪落無聲之願",
            "main_stats": [
                {"name": "冷凝傷害加成", "value": 27.1, "unit": "%", "stat_kind": "primary"},
                {"name": "攻擊", "value": 90, "unit": "", "stat_kind": "fixed"},
            ],
            "sub_stats": [
                {"name": name, "value": value, "unit": unit, "confidence": 1, "slot_index": i}
                for i, (name, value, unit) in enumerate(
                    [
                        ("生命", 510, ""),
                        ("共鳴技能傷害加成", 9.4, "%"),
                        ("防禦", 60, ""),
                        ("普攻傷害加成", 7.1, "%"),
                    ],
                    1,
                )
            ],
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=3,
    )
    assert result.evaluation is not None
    assert result.evaluation.breakdown.score == pytest.approx(14.93)
    assert result.target_comparable and result.delta == pytest.approx(14.93 - baseline)
    if missing_quality_probability:
        assert result.conclusion == "資料待補"
        assert result.evaluation.decision.requires_manual_confirmation
    else:
        assert result.conclusion == (
            "已超過現有, 建議停止投入" if baseline < 14.93 else "建議停止投入"
        )
        assert result.evaluation.decision.action is UpgradeAction.STOP
        assert "可用 55.0 分" in result.reason
    assert _dump(equipment.database) == before


def test_usable_replacement_below_passing_can_still_continue(
    equipment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring import UpgradeAction

    owner = _guide_equipment(equipment)
    service = ReplacementTrainingService(equipment.database.session_factory)
    target = service.target(equipment.data_set_id, owner, 1)
    target = replace(target, piece=replace(target.piece, score=10))
    monkeypatch.setattr(service, "target", lambda *args: target)
    stats = _stats()[:3]
    stats[2].update(name="防禦", value=11.8)
    result = service.evaluate(
        {
            "level": 15,
            "cost": 1,
            "set_name": "雪落無聲之願",
            "main_stats": list(target.piece.main_stats),
            "sub_stats": stats,
        },
        data_set_id=equipment.data_set_id,
        character_id=owner,
        slot_index=1,
    )
    assert result.evaluation is not None
    assert 55 <= result.evaluation.breakdown.score < 70
    assert result.evaluation.decision.action is UpgradeAction.CONTINUE
    assert "停止" not in result.conclusion
