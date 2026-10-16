from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
)
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.onboarding import (
    CAPTURE_MONITOR_SETTING_KEY,
    ONBOARDING_STATE_KEY,
    SCORING_TEMPLATE_SETTING_KEY,
    DemoDataService,
    OnboardingCommand,
    OnboardingMode,
    OnboardingService,
)
from wuwa_assistant.storage import AppPaths, Database, create_database
from wuwa_assistant.storage.orm import (
    CharacterRecord,
    CharacterSkillRecord,
    DataSetRecord,
    TeamMemberRecord,
    TeamRecord,
)

COMPLETED_AT = datetime(2026, 8, 8, 4, 30, tzinfo=UTC)


def _service(database: Database, root: Path) -> OnboardingService:
    return OnboardingService(
        database.session_factory,
        database.settings,
        data_path=root,
        now=lambda: COMPLETED_AT,
    )


def test_first_character_completion_is_idempotent_and_restart_persistent(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "onboarding-primary")
    database = create_database(paths)
    service = _service(database, paths.root)
    command = OnboardingCommand(
        monitor_identifier="monitor-2",
        data_path=paths.root,
        mode=OnboardingMode.FIRST_CHARACTER,
        safety_confirmed=True,
        capture_test_confirmed=True,
        data_path_confirmed=True,
        character_name="我的第一名角色",
        scoring_template_kind="energy_requirement",
    )
    try:
        first = service.complete(command)
        second = service.complete(command)

        assert first.completed
        assert first.completed_at == COMPLETED_AT
        assert first.data_set_id == second.data_set_id
        assert first.character_id == second.character_id
        assert first.mode is OnboardingMode.FIRST_CHARACTER
        assert first.scoring_template_kind == "energy_requirement"
        assert database.settings.get(CAPTURE_MONITOR_SETTING_KEY) == "monitor-2"
        assert database.settings.get(SCORING_TEMPLATE_SETTING_KEY) == "energy_requirement"
        characters = CharacterCultivationService(database.session_factory).list_characters(
            data_set_id=first.data_set_id or ""
        )
        assert [item.name for item in characters] == ["我的第一名角色"]

        restarted = _service(database, paths.root).load_state()
        assert restarted == second
        assert restarted.data_path == paths.root
    finally:
        database.dispose()


def test_quick_start_completion_creates_no_character(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "onboarding-quick-start")
    database = create_database(paths)
    service = _service(database, paths.root)
    try:
        state = service.complete(
            OnboardingCommand(
                monitor_identifier="monitor-1",
                data_path=paths.root,
                mode=OnboardingMode.QUICK_START,
                safety_confirmed=True,
                capture_test_confirmed=True,
                data_path_confirmed=True,
            )
        )

        assert state.completed
        assert state.mode is OnboardingMode.QUICK_START
        assert state.data_set_id is not None
        assert state.character_id is None
        characters = CharacterCultivationService(database.session_factory).list_characters(
            data_set_id=state.data_set_id
        )
        assert characters == ()
        assert service.load_state() == state
    finally:
        database.dispose()


def test_demo_seed_is_fictional_idempotent_and_clear_never_touches_primary(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "onboarding-demo")
    database = create_database(paths)
    primary_id = EchoHistoryService(database.session_factory).ensure_active_primary_dataset()
    CharacterCultivationService(database.session_factory).create_character(
        data_set_id=primary_id,
        command=CreateCharacter(name="主要資料角色"),
    )
    demos = DemoDataService(database.session_factory)
    try:
        first = demos.ensure_demo_data()
        second = demos.ensure_demo_data()

        assert first.created_new_dataset
        assert not second.created_new_dataset
        assert first.data_set_id == second.data_set_id
        assert second.character_count == 2
        assert second.team_count == 1
        with database.session_factory() as session:
            demo = session.get(DataSetRecord, first.data_set_id)
            assert demo is not None
            assert demo.kind == "demo"
            assert not demo.is_active
            assert (
                session.scalar(
                    select(func.count(CharacterRecord.id)).where(
                        CharacterRecord.data_set_id == first.data_set_id
                    )
                )
                == 2
            )
            assert (
                session.scalar(
                    select(func.count(CharacterSkillRecord.id))
                    .join(CharacterRecord)
                    .where(CharacterRecord.data_set_id == first.data_set_id)
                )
                == 10
            )
            team_ids = select(TeamRecord.id).where(TeamRecord.data_set_id == first.data_set_id)
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(TeamMemberRecord)
                    .where(TeamMemberRecord.team_id.in_(team_ids))
                )
                == 2
            )

        assert demos.clear_demo_data() == 1
        assert demos.clear_demo_data() == 0
        with database.session_factory() as session:
            assert (
                session.scalar(
                    select(func.count(DataSetRecord.id)).where(DataSetRecord.kind == "demo")
                )
                == 0
            )
            primary = session.get(DataSetRecord, primary_id)
            assert primary is not None and primary.kind == "primary" and primary.is_active
            assert (
                session.scalar(
                    select(func.count(CharacterRecord.id)).where(
                        CharacterRecord.data_set_id == primary_id
                    )
                )
                == 1
            )
    finally:
        database.dispose()


def test_demo_onboarding_selects_demo_partition_without_deactivating_primary(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "onboarding-demo-mode")
    database = create_database(paths)
    primary_id = EchoHistoryService(database.session_factory).ensure_active_primary_dataset()
    service = _service(database, paths.root)
    try:
        state = service.complete(
            OnboardingCommand(
                monitor_identifier="monitor-1",
                data_path=paths.root,
                mode=OnboardingMode.DEMO_DATA,
                safety_confirmed=True,
                capture_test_confirmed=True,
                data_path_confirmed=True,
            )
        )

        assert state.completed and state.mode is OnboardingMode.DEMO_DATA
        assert state.character_id is None
        assert state.data_set_id != primary_id
        with database.session_factory() as session:
            selected = session.get(DataSetRecord, state.data_set_id)
            primary = session.get(DataSetRecord, primary_id)
            assert selected is not None and selected.kind == "demo" and not selected.is_active
            assert primary is not None and primary.is_active
    finally:
        database.dispose()


def test_invalid_or_future_onboarding_state_is_not_guessed(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "onboarding-invalid")
    database = create_database(paths)
    try:
        database.settings.set(
            ONBOARDING_STATE_KEY,
            {"schema_version": 99, "completed": True},
        )
        state = _service(database, paths.root).load_state()
        assert not state.completed
        assert state.invalid_reason == "onboarding state version is not supported"

        wrong_path = AppPaths.from_root(tmp_path / "somewhere-else").root
        with pytest.raises(ValueError, match="does not match"):
            _service(database, paths.root).complete(
                OnboardingCommand(
                    monitor_identifier="monitor-1",
                    data_path=wrong_path,
                    mode=OnboardingMode.DEMO_DATA,
                    safety_confirmed=True,
                    capture_test_confirmed=True,
                    data_path_confirmed=True,
                )
            )
        assert database.settings.get(ONBOARDING_STATE_KEY)["schema_version"] == 99
    finally:
        database.dispose()
