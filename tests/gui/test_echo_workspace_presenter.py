from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from sqlalchemy import func, select

from wuwa_assistant.application import (
    EchoHistoryService,
    IngestReceipt,
    ObservationSnapshot,
    ScoringProfileService,
    ScoringWorkflowService,
)
from wuwa_assistant.application.characters import CharacterCultivationService
from wuwa_assistant.capture import CaptureStatus
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import UpgradeDecisionRecord
from wuwa_assistant.ui.controllers.characters import CharactersPresenter
from wuwa_assistant.ui.controllers.echo_workspace import (
    EchoWorkspacePresenter,
    EchoWorkspaceSettings,
    _capture_run_heading,
    _Ingested,
    _JobResult,
    _last_known_equipped,
    _last_known_equipped_owner,
    _last_known_set,
    _observation_view,
    _Operation,
    _SessionJobContext,
)
from wuwa_assistant.ui.controllers.live_capture import (
    CaptureControllerError,
    CaptureStatusView,
    ControllerErrorCode,
    LiveCaptureSettings,
    MonitorDescriptor,
    RecognitionPayload,
)
from wuwa_assistant.ui.pages.characters import CharactersPage
from wuwa_assistant.ui.pages.echo_live import CorrectionRequest, EchoLivePage
from wuwa_assistant.ui.pages.history import EchoHistoryPage
from wuwa_assistant.ui.pages.scoring_profiles import ScoringProfilesPage


class _FakeLiveController(QObject):
    monitors_changed = Signal(object)
    status_changed = Signal(object)
    recognition_ready = Signal(object)
    error_occurred = Signal(object)
    running_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.started_with: LiveCaptureSettings | None = None
        self.start_count = 0
        self.stop_count = 0
        self.shutdown_count = 0
        self.recheck_count = 0
        self.result_floor = -1

    def request_recheck(self) -> bool:
        self.recheck_count += 1
        return True

    def accepts_recognition(self, value: RecognitionPayload) -> bool:
        return value.source_sequence > self.result_floor

    def refresh_monitors(self) -> bool:
        self.monitors_changed.emit(
            (
                MonitorDescriptor(
                    identifier="monitor-1",
                    index=1,
                    label="測試顯示器 — 2560x1440",
                    left=0,
                    top=0,
                    width=2560,
                    height=1440,
                    is_primary=True,
                    is_compatible=True,
                ),
            )
        )
        return True

    def start(self, settings: LiveCaptureSettings) -> bool:
        self.started_with = settings
        self.start_count += 1
        self.status_changed.emit(
            CaptureStatusView(CaptureStatus.WAITING_FOR_ECHO_PAGE, "等待聲骸頁面")
        )
        self.running_changed.emit(True)
        return True

    def stop(self) -> bool:
        self.stop_count += 1
        self.status_changed.emit(CaptureStatusView(CaptureStatus.STOPPED, "已停止"))
        self.running_changed.emit(False)
        return True

    def shutdown(self, *, timeout: float = 8.0) -> bool:
        del timeout
        self.shutdown_count += 1
        return True


def test_capture_run_heading_converts_utc_to_taipei_time() -> None:
    assert _capture_run_heading("2026-08-24T14:34:00+00:00") == "本次辨識 · 2026-08-24 22:34 開始"


def test_replacement_target_loads_offline_and_clear_rejects_late_result(
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
    monkeypatch,
) -> None:
    from threading import Event

    from wuwa_assistant.application.characters import CreateCharacter
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions

    database, history = history_storage
    dataset = history.ensure_active_primary_dataset()
    ScoringProfileService(database.session_factory).ensure_system_templates(
        load_character_seed_definitions()
    )
    owner = (
        CharacterCultivationService(database.session_factory)
        .create_character(data_set_id=dataset, command=CreateCharacter(name="緋雪"))
        .summary.id
    )
    replacement = ReplacementTrainingService(database.session_factory)
    live = _FakeLiveController()
    page = EchoLivePage()
    qtbot.addWidget(page)
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=history,
        echo_page=page,
        history_page=EchoHistoryPage(),
        replacement_service=replacement,
        data_set_id=dataset,
        app_version="test",
    )
    release = Event()
    started = Event()
    original = replacement.target
    try:
        with qtbot.waitSignal(presenter.training_ready):
            presenter.set_training_target(owner, 2)
        assert "緋雪 · 位置 2" in page.training_label.text()
        assert live.start_count == 0 and presenter._active_character_id is None

        def slow_target(*args):
            started.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(replacement, "target", slow_target)
        presenter.set_training_target(owner, 3)
        qtbot.waitUntil(started.is_set)
        presenter.clear_training_target()
        release.set()
        qtbot.waitUntil(lambda: not presenter._futures)
        qtbot.wait(20)
        assert presenter._training_target is None
        assert page.training_panel.isHidden()
        assert live.start_count == 0
    finally:
        release.set()
        presenter.shutdown()


def test_training_comparison_does_not_write_decisions_or_assign_candidate(
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
) -> None:
    from wuwa_assistant.application.characters import CreateCharacter
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions

    database, history = history_storage
    dataset = _seed_grouped_history(history)
    profiles = ScoringProfileService(database.session_factory)
    profiles.ensure_system_templates(load_character_seed_definitions())
    owner = (
        CharacterCultivationService(database.session_factory)
        .create_character(data_set_id=dataset, command=CreateCharacter(name="緋雪"))
        .summary.id
    )
    page = EchoLivePage()
    qtbot.addWidget(page)
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),
        history_service=history,
        echo_page=page,
        history_page=EchoHistoryPage(),
        scoring_workflow=ScoringWorkflowService(database.session_factory, profiles),
        replacement_service=ReplacementTrainingService(database.session_factory),
        data_set_id=dataset,
        app_version="test",
    )
    try:
        with qtbot.waitSignal(presenter.training_ready):
            presenter.set_training_target(owner, 1)
        summary = history.list_history(data_set_id=dataset)[0]
        snapshot = history.get_history(summary.id)
        presenter._apply_history(
            snapshot, update_live=True, evaluate_score=True, persist_score=True
        )
        qtbot.waitUntil(lambda: not presenter._futures)
        qtbot.wait(20)
        assert "緋雪" in page.training_label.text()
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 0
        assert history.get_history(summary.id).session.character_id is None
        assert presenter._active_character_id is None
    finally:
        presenter.shutdown()


def test_set_name_survives_an_enhancement_page_without_set_text():
    observations = tuple(
        ObservationSnapshot(
            id=str(index),
            sequence_no=index,
            observed_at="2026-09-21T00:00:00Z",
            raw_payload={},
            effective_payload=payload,
            requires_review=False,
        )
        for index, payload in enumerate(
            [
                {"page_type": "echo_selection", "set_name": "雪落無聲之願"},
                {"page_type": "echo_enhancement", "set_name": None},
            ],
            start=1,
        )
    )
    view = _observation_view(
        "session",
        "2",
        observations[-1].effective_payload,
        False,
        set_fallback=_last_known_set(observations),
    )
    assert view.set_name == "雪落無聲之願"


def test_event_only_revisit_refreshes_score_without_persisting_decision(
    qapp: QApplication,
    history_storage: tuple[Database, EchoHistoryService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qapp
    _database, service = history_storage
    data_set_id = service.ensure_active_primary_dataset()
    capture = service.start_capture(
        data_set_id=data_set_id,
        backend_kind="mss_monitor",
        monitor_index=1,
        sampling_hz=5.0,
        stable_wait_ms=600,
        app_version="test",
        roi_config_version="test",
    )
    initial = service.ingest_live_result(capture.id, _recognition().observation, source_sequence=10)
    assert initial.echo_session_id is not None
    history = service.get_history(initial.echo_session_id)
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),  # type: ignore[arg-type]
        history_service=service,
        echo_page=EchoLivePage(),
        history_page=EchoHistoryPage(),
        app_version="test",
    )
    calls: list[dict[str, bool]] = []

    def capture_apply_history(
        _history: object,
        *,
        update_live: bool,
        evaluate_score: bool,
        persist_score: bool,
    ) -> None:
        calls.append(
            {
                "update_live": update_live,
                "evaluate_score": evaluate_score,
                "persist_score": persist_score,
            }
        )

    monkeypatch.setattr(presenter, "_apply_history", capture_apply_history)
    monkeypatch.setattr(presenter, "refresh_history", lambda: None)
    presenter._apply_ingested(
        _Ingested(
            receipt=IngestReceipt(capture.id, history.session.id, None, ("event-1",), False),
            history=history,
            recognition=_recognition(),
        )
    )

    assert calls == [{"update_live": True, "evaluate_score": True, "persist_score": False}]
    assert presenter.shutdown()


@pytest.fixture
def history_storage(tmp_path: Path) -> Iterator[tuple[Database, EchoHistoryService]]:
    database = create_database(AppPaths.from_root(tmp_path / "workspace"))
    try:
        yield database, EchoHistoryService(database.session_factory)
    finally:
        database.dispose()


def _recognition() -> RecognitionPayload:
    confidence = 0.96
    stat = {
        "stat_kind": "primary",
        "name": "生命",
        "value": 8.2,
        "unit": "%",
        "confidence": confidence,
        "name_confidence": confidence,
        "value_confidence": confidence,
        "raw_name": "生命",
        "raw_value": "8.2%",
        "slot_index": None,
    }
    fixed_stat = {
        **stat,
        "stat_kind": "fixed",
        "value": 820,
        "unit": None,
        "raw_value": "820",
        "slot_index": 1,
    }
    observation = {
        "page_type": "echo_enhancement",
        "name": "不熄獵手",
        "level": 5,
        "projected_level": None,
        "cost": 1,
        "main_stats": [stat, fixed_stat],
        "projected_main_stats": [],
        "sub_stats": [{**stat, "stat_kind": "substat", "name": "暴擊", "value": 7.5}],
        "locked_substat_slots": 4,
        "confidence": {"page_type": confidence, "name": confidence, "level": confidence},
        "source_timestamp": "2026-08-01T12:00:00+00:00",
        "source_file": None,
        "is_discarded": False,
        "material_source_selected": None,
        "material_source_level": None,
        "raw_ocr": {},
        "debug_roi_paths": [],
        "debug_ocr_path": None,
        "semantic_fingerprint": "echo-session-0001:5",
        "current_session": {
            "local_session_id": "echo-session-0001",
            "current_state": "active",
            "is_discarded": False,
            "was_consumed": False,
            "consumed_into_session_id": None,
            "latest_level": 5,
        },
        "events": [
            {
                "event_type": "echo_session_started",
                "session_id": "echo-session-0001",
                "related_session_id": None,
                "previous_observation": None,
                "current_observation": {"page_type": "echo_enhancement", "level": 5},
                "confidence": confidence,
                "notes": ["session_started"],
                "previous_level": None,
                "current_level": 5,
                "new_substats": [],
                "source_file": None,
            }
        ],
    }
    return RecognitionPayload(
        generation=1,
        source_sequence=10,
        captured_at="2026-08-01T12:00:00+00:00",
        completed_at=1.0,
        packet_kind="content",
        state_fingerprint="state-5",
        page_type="echo_enhancement",
        relevant=True,
        confidence=confidence,
        observation=observation,
    )


def _seed_grouped_history(service: EchoHistoryService) -> str:
    dataset = service.ensure_active_primary_dataset()
    for index, name in enumerate(("較早聲骸", "重複聲骸", "重複聲骸", "較新聲骸"), start=1):
        capture = service.start_capture(
            data_set_id=dataset,
            backend_kind="test",
            monitor_index=1,
            sampling_hz=5.0,
            stable_wait_ms=600,
            app_version="test",
        )
        payload = deepcopy(_recognition().observation)
        payload["name"] = name
        payload["events"] = []
        service.ingest_live_result(capture.id, payload, source_sequence=index)
        service.stop_capture(capture.id)
    return dataset


def test_history_folds_by_default_and_checkbox_restores_every_scan(
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = history_storage
    dataset = _seed_grouped_history(service)
    history_page = EchoHistoryPage()
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),  # type: ignore[arg-type]
        history_service=service,
        echo_page=EchoLivePage(),
        history_page=history_page,
        data_set_id=dataset,
        app_version="test",
    )
    try:
        assert history_page.group_duplicates_check.isChecked()
        presenter.refresh_history()
        qtbot.waitUntil(lambda: history_page.session_model.rowCount() == 3)
        assert sum(row.rescan_count for row in presenter._history_rows) == 1
        assert history_page.status_label.text() == "4 筆掃描 · 3 組聲骸"
        assert any(
            "掃描 2 次" in str(history_page.session_model.index(row, 2).data()) for row in range(3)
        )
        history_page.group_duplicates_check.setChecked(False)
        qtbot.waitUntil(lambda: history_page.session_model.rowCount() == 4)
        assert presenter._history_rows == service.list_history(data_set_id=dataset)
        assert "共 4 筆掃描" in history_page.status_label.text()
        history_page.group_duplicates_check.setChecked(True)
        qtbot.waitUntil(lambda: history_page.session_model.rowCount() == 3)
        assert len(service.list_history(data_set_id=dataset)) == 4
    finally:
        assert presenter.shutdown()


@pytest.mark.parametrize("current_grouping", [False, True])
@pytest.mark.parametrize("stale_error", [False, True])
def test_stale_history_result_cannot_overwrite_the_current_grouping_mode(
    qapp: QApplication,
    history_storage: tuple[Database, EchoHistoryService],
    monkeypatch: pytest.MonkeyPatch,
    current_grouping: bool,
    stale_error: bool,
) -> None:
    del qapp
    _database, service = history_storage
    dataset = _seed_grouped_history(service)
    history_page = EchoHistoryPage()
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),  # type: ignore[arg-type]
        history_service=service,
        echo_page=EchoLivePage(),
        history_page=history_page,
        data_set_id=dataset,
        app_version="test",
    )
    monkeypatch.setattr(presenter, "_submit", lambda *_args, **_kwargs: None)
    try:
        history_page.group_duplicates_check.setChecked(current_grouping)
        current = service.list_history(data_set_id=dataset, group_duplicates=current_grouping)
        presenter._handle_job_result(
            _JobResult(_Operation.HISTORY_LIST, current, context=current_grouping)
        )
        status = history_page.status_label.text()
        stale = service.list_history(data_set_id=dataset, group_duplicates=not current_grouping)
        presenter._handle_job_result(
            _JobResult(
                _Operation.HISTORY_LIST,
                stale,
                context=not current_grouping,
                error="stale failure" if stale_error else None,
            )
        )
        assert presenter._history_rows == current
        assert history_page.session_model.rowCount() == len(current)
        assert history_page.status_label.text() == status
    finally:
        assert presenter.shutdown()


def test_history_navigation_resolves_a_live_member_to_its_adjacent_group(
    qapp: QApplication,
    history_storage: tuple[Database, EchoHistoryService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qapp
    _database, service = history_storage
    dataset = _seed_grouped_history(service)
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),  # type: ignore[arg-type]
        history_service=service,
        echo_page=EchoLivePage(),
        history_page=EchoHistoryPage(),
        data_set_id=dataset,
        app_version="test",
    )
    targets: list[str] = []
    monkeypatch.setattr(presenter, "load_history_detail", targets.append)
    try:
        rows = service.list_history(data_set_id=dataset, group_duplicates=True)
        assert len(rows) == 3 and rows[1].rescan_count == 1
        presenter._apply_history_list(rows)
        member = next(
            identifier for identifier in rows[1].grouped_session_ids if identifier != rows[1].id
        )
        presenter._current_db_session_id = member
        presenter.navigate_session(1)
        presenter.navigate_session(-1)
        assert targets == [rows[2].id, rows[0].id]
        assert presenter._current_db_session_id == member
    finally:
        assert presenter.shutdown()


@pytest.mark.parametrize("grouped", [True, False])
def test_history_reads_one_snapshot_without_silently_truncating_scans(
    qapp: QApplication,
    history_storage: tuple[Database, EchoHistoryService],
    monkeypatch: pytest.MonkeyPatch,
    grouped: bool,
) -> None:
    del qapp
    _database, service = history_storage
    dataset = _seed_grouped_history(service)
    sample = service.list_history(data_set_id=dataset)[0]
    rows = tuple(replace(sample, id=f"scan-{index}") for index in range(1001))
    calls: list[bool] = []

    def list_snapshot(*, data_set_id: str, group_duplicates: bool):
        assert data_set_id == dataset and group_duplicates is grouped
        calls.append(group_duplicates)
        return rows

    monkeypatch.setattr(service, "list_history_snapshot", list_snapshot)
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),  # type: ignore[arg-type]
        history_service=service,
        echo_page=EchoLivePage(),
        history_page=EchoHistoryPage(),
        data_set_id=dataset,
        app_version="test",
    )
    try:
        assert presenter._load_history_rows(grouped) == rows
        assert calls == [grouped]
    finally:
        assert presenter.shutdown()


def test_repeated_history_navigation_advances_without_changing_live_item(
    qapp: QApplication,
    history_storage: tuple[Database, EchoHistoryService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qapp
    _database, service = history_storage
    dataset = _seed_grouped_history(service)
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),  # type: ignore[arg-type]
        history_service=service,
        echo_page=EchoLivePage(),
        history_page=EchoHistoryPage(),
        data_set_id=dataset,
        app_version="test",
    )
    monkeypatch.setattr(presenter, "_submit", lambda *_args, **_kwargs: None)
    try:
        rows = service.list_history(data_set_id=dataset, group_duplicates=True)
        presenter._apply_history_list(rows)
        presenter._current_db_session_id = rows[0].id
        presenter.navigate_session(1)
        assert presenter._history_navigation_id == rows[1].id
        presenter.navigate_session(1)
        assert presenter._history_navigation_id == rows[2].id
        assert presenter._current_db_session_id == rows[0].id
    finally:
        assert presenter.shutdown()


def test_last_known_equipped_evidence_survives_a_later_page_without_badge() -> None:
    def observation(sequence: int, equipped: bool | None) -> ObservationSnapshot:
        return ObservationSnapshot(
            id=f"observation-{sequence}",
            sequence_no=sequence,
            observed_at=f"2026-08-24T12:00:0{sequence}+00:00",
            requires_review=False,
            raw_payload={},
            effective_payload={"is_equipped": equipped},
        )

    assert _last_known_equipped((observation(1, True), observation(2, None))) is True
    assert _last_known_equipped((observation(1, False), observation(2, None))) is False
    assert _last_known_equipped((observation(1, None),)) is None


@pytest.mark.parametrize(
    ("equipped", "owner", "expected_owner"),
    [(None, None, "愛彌斯"), (False, "愛彌斯", None), (True, None, None), (True, "長離", "長離")],
)
def test_equipped_owner_fallback_respects_latest_explicit_evidence(
    equipped: bool | None, owner: str | None, expected_owner: str | None
) -> None:
    def observation(sequence: int, equipped: bool | None, owner: str | None) -> ObservationSnapshot:
        return ObservationSnapshot(
            id=f"observation-{sequence}",
            sequence_no=sequence,
            observed_at=f"2026-09-20T12:00:0{sequence}+00:00",
            requires_review=False,
            raw_payload={},
            effective_payload={"is_equipped": equipped, "equipped_character_name": owner},
        )

    history = (
        observation(1, True, "愛彌斯"),
        observation(2, equipped, owner),
        observation(3, None, None),
    )
    owner_fallback = _last_known_equipped_owner(history)
    assert owner_fallback == expected_owner
    view = _observation_view(
        "session-1",
        history[-1].id,
        history[-1].effective_payload,
        False,
        equipped_fallback=_last_known_equipped(history),
        owner_fallback=owner_fallback,
    )
    assert view.equipped_character_name == expected_owner
    assert view.is_equipped == (True if equipped is None else equipped)


@pytest.mark.parametrize("recognized_slot", [None, 3])
def test_new_owner_and_keep_slot_flow_updates_character_page_without_reselecting_echo(
    qapp: QApplication,
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
    recognized_slot: int | None,
) -> None:
    del qapp
    database, history_service = history_storage
    data_set_id = history_service.ensure_active_primary_dataset()
    character_service = CharacterCultivationService(database.session_factory)
    profile_service = ScoringProfileService(database.session_factory)
    profile_service.ensure_system_templates(load_template_seed_definitions())
    echo_page = EchoLivePage()
    characters_page = CharactersPage()
    history_page = EchoHistoryPage()
    for page in (echo_page, characters_page, history_page):
        qtbot.addWidget(page)
    live = _FakeLiveController()
    presenter = EchoWorkspacePresenter(
        live_controller=live,  # type: ignore[arg-type]
        history_service=history_service,
        echo_page=echo_page,
        history_page=history_page,
        data_set_id=data_set_id,
        app_version="test",
    )
    characters_presenter = CharactersPresenter(
        service=character_service,
        scoring_profiles=profile_service,
        page=characters_page,
        data_set_id=data_set_id,
    )
    # Use the production signal wiring: the character page may already be open
    # when a newly visible owner is added or a permanent slot is saved.
    presenter.characters_changed.connect(characters_presenter.refresh_external_changes)
    characters_presenter.character_choices_changed.connect(echo_page.set_characters)
    notifications: list[None] = []
    presenter.characters_changed.connect(lambda: notifications.append(None))
    try:
        characters_presenter.initialize()
        presenter.initialize()
        qtbot.waitUntil(characters_page.add_button.isEnabled)
        qtbot.waitUntil(lambda: echo_page.monitor_combo.currentData() == "monitor-1")
        assert characters_page.character_model.rowCount() == 0
        presenter.start_capture("monitor-1", "", "")
        qtbot.waitUntil(lambda: presenter._active_capture_id is not None)

        recognition = _recognition()
        live.recognition_ready.emit(
            replace(
                recognition,
                page_type="echo_selection",
                observation={
                    **recognition.observation,
                    "page_type": "echo_selection",
                    "name": "冠頂械隼",
                    "is_equipped": True,
                    "equipped_character_name": "未收錄的新角色",
                    "equipped_slot_index": recognized_slot,
                    "set_name": "雪落無聲之願",
                    "confidence": {
                        "page_type": 0.99,
                        "name": 0.99,
                        "level": 0.99,
                        "equipped_character_name": 0.99,
                        "equipped_slot_index": 0.99,
                    },
                    # Explicitly cover roster refresh via created_character_id,
                    # without depending on an unrelated tracker event.
                    "events": [],
                },
            )
        )
        qtbot.waitUntil(lambda: characters_page.character_model.rowCount() == 1)
        owner = character_service.list_characters(data_set_id=data_set_id)[0]
        assert owner.name == "未收錄的新角色"
        qtbot.waitUntil(lambda: characters_page.current_character_id() == owner.id)
        qtbot.waitUntil(lambda: echo_page.result_character_combo.currentData() == owner.id)
        assert notifications == [None]
        assert echo_page.set_label.text() == "雪落無聲之願"
        assert echo_page.result_slot_combo.currentData() == (recognized_slot or 0)
        if recognized_slot is None:
            assert "冠頂械隼" in characters_page.unpositioned_echoes_label.text()
            assert not characters_page.unpositioned_echoes_label.isHidden()
            # Unknown slots remain visible; a manual choice promotes this record.
            echo_page.result_slot_combo.setCurrentIndex(echo_page.result_slot_combo.findData(3))
            assert echo_page.keep_button.text() == "保留並套用位置"
            echo_page.keep_button.click()
        qtbot.waitUntil(lambda: characters_page.echo_cards[2].name_label.text() == "冠頂械隼")
        detail = character_service.get_character(owner.id, data_set_id=data_set_id)
        assert detail.echo_slots[2].echo_id is not None
        assert detail.echo_slots[2].name == "冠頂械隼"
        assert detail.echo_slots[2].final_status == "equipped"
        assert detail.echo_slots[2].set_name == "雪落無聲之願"
        assert detail.unpositioned_echoes == ()
        assert characters_page.unpositioned_echoes_label.isHidden()
        assert all(
            slot.echo_id is None for index, slot in enumerate(detail.echo_slots) if index != 2
        )
        summary = history_service.list_history(data_set_id=data_set_id)[0]
        assert (summary.character_id, summary.slot_index) == (owner.id, 3)
        assert summary.final_status == "equipped"
        assert characters_page.current_character_id() == owner.id
        assert echo_page.result_character_combo.currentData() == owner.id
        assert echo_page.result_slot_combo.currentData() == 3
        assert notifications == ([None, None] if recognized_slot is None else [None])
    finally:
        assert presenter.shutdown()
        assert characters_presenter.shutdown()


def _page_probe(
    page_type: str,
    *,
    confidence: float,
    relevant: bool = False,
) -> RecognitionPayload:
    return RecognitionPayload(
        generation=1,
        source_sequence=9,
        captured_at="2026-08-01T11:59:59+00:00",
        completed_at=0.9,
        packet_kind="probe",
        state_fingerprint="probe-state",
        page_type=page_type,
        relevant=relevant,
        confidence=confidence,
        observation={
            "page_type": page_type,
            "confidence": {"page_type": confidence},
            "events": [],
        },
    )


def test_presenter_persists_live_result_correction_and_stop_without_ui_database_work(
    qapp: QApplication,
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
    caplog: pytest.LogCaptureFixture,
    request: pytest.FixtureRequest,
) -> None:
    del qapp
    _database, service = history_storage
    data_set_id = service.ensure_active_primary_dataset()
    profile_service = ScoringProfileService(_database.session_factory)
    profile_service.ensure_system_templates(load_template_seed_definitions())
    default_profile = profile_service.get_default(data_set_id=data_set_id)
    assert default_profile is not None
    live = _FakeLiveController()
    echo_page = EchoLivePage()
    history_page = EchoHistoryPage()
    scoring_page = ScoringProfilesPage()
    presenter = EchoWorkspacePresenter(
        live_controller=live,  # type: ignore[arg-type]
        history_service=service,
        echo_page=echo_page,
        history_page=history_page,
        scoring_workflow=ScoringWorkflowService(_database.session_factory, profile_service),
        scoring_preview_page=scoring_page,
        app_version="0.4.0",
        settings=EchoWorkspaceSettings(),
    )
    # Stop queued DB/scoring work before fixture teardown deletes its pages,
    # including when a wait or assertion fails before the explicit shutdown.
    request.addfinalizer(presenter.shutdown)
    caplog.set_level("INFO", logger="wuwa_assistant")

    presenter.initialize()
    qtbot.waitUntil(lambda: echo_page.monitor_combo.currentData() == "monitor-1")
    presenter._session_db_by_local["echo-session-0001"] = "old-capture-session"
    echo_page.start_requested.emit("monitor-1", "", default_profile.id)
    qtbot.waitUntil(lambda: live.started_with is not None)
    assert live.started_with is not None and live.started_with.monitor_index == 1
    assert presenter._session_db_by_local == {}
    assert not echo_page.character_combo.isEnabled()
    assert not echo_page.profile_combo.isEnabled()

    # Page probes are user-facing diagnostics, not durable echo observations.  A
    # common first screen is the character overview; tell the user exactly what
    # was seen and how to continue instead of silently waiting forever.
    live.recognition_ready.emit(_page_probe("character_echo_overview", confidence=0.998))
    qtbot.waitUntil(lambda: "角色聲骸總覽" in echo_page.page_detection_label.text())
    assert "100%" in echo_page.confidence_label.text()
    assert service.list_history() == ()

    transient = _recognition()
    live.recognition_ready.emit(
        replace(
            transient,
            source_sequence=9,
            completed_at=0.95,
            observation={
                **transient.observation,
                "name": "single-glyph-fragment",
                "level": None,
                "main_stats": [],
                "sub_stats": [],
            },
        )
    )
    assert service.list_history() == ()

    live.recognition_ready.emit(_recognition())
    qtbot.waitUntil(lambda: echo_page.name_label.text() == "不熄獵手")
    # This integration uses real remaining-roll enumeration and persistence;
    # the default five seconds is too close to a cold run on a shared CI host.
    qtbot.waitUntil(lambda: echo_page.score_label.text() != "已開詞條品質 —/100", timeout=30_000)
    qtbot.waitUntil(lambda: scoring_page.score_label.text() != "—")
    assert echo_page.conclusion_label.text() != "等待評分設定檔"
    assert scoring_page.breakdown_model.rowCount() == 1
    substat_model = echo_page.sub_stats_table.model()
    assert substat_model is not None
    assert substat_model.data(substat_model.index(0, 2)) != "—"
    assert substat_model.data(substat_model.index(0, 3)) != "—"
    qtbot.waitUntil(lambda: len(service.list_history()) == 1)
    summary = service.list_history()[0]
    history = service.get_history(summary.id)
    observation_id = history.observations[-1].id
    assert history.observations[-1].raw_payload["semantic_fingerprint"] == "state-5"

    live.recognition_ready.emit(
        replace(
            _recognition(),
            source_sequence=11,
            completed_at=1.2,
            unchanged=True,
        )
    )
    qtbot.waitUntil(lambda: not presenter._futures)
    unchanged_history = service.get_history(summary.id)
    assert len(unchanged_history.observations) == 1
    with _database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 1

    presenter.correct_observation(
        CorrectionRequest(
            session_id="echo-session-0001",
            observation_id=observation_id,
            field_path="/name",
            previous_value="不熄獵手",
            corrected_value="人工確認名稱",
        )
    )
    qtbot.waitUntil(
        lambda: (
            service.get_history(summary.id).observations[-1].effective_payload["name"]
            == "人工確認名稱"
        )
    )
    assert service.get_history(summary.id).corrections[0].old_value == "不熄獵手"

    # Persisting the correction precedes its real score recomputation. Drain that
    # work explicitly before disposition/stop assertions use the short UI waits.
    qtbot.waitUntil(
        lambda: (
            echo_page.name_label.text() == "人工確認名稱"
            and presenter._score_in_flight is None
            and not presenter._futures
        ),
        timeout=30_000,
    )

    presenter.set_disposition("echo-session-0001", "kept")
    qtbot.waitUntil(lambda: service.list_history()[0].final_status == "kept")

    # A slow action for the previous echo must never replace a newer live
    # result when its worker callback returns later.
    presenter._current_db_session_id = "newer-session"
    presenter._handle_job_result(
        _JobResult(
            _Operation.DISPOSITION,
            service.get_history(summary.id),
            context=_SessionJobContext(summary.id),
        )
    )
    assert presenter._current_db_session_id == "newer-session"

    presenter.stop_capture()
    qtbot.waitUntil(lambda: live.stop_count == 1)
    qtbot.waitUntil(
        lambda: echo_page.character_combo.isEnabled() and echo_page.profile_combo.isEnabled()
    )
    qtbot.waitUntil(lambda: not presenter._futures)
    with _database.session_factory() as session:
        decision_count = session.scalar(select(func.count()).select_from(UpgradeDecisionRecord))
    generation = presenter._score_generation
    presenter.load_history_detail(summary.id)
    qtbot.waitUntil(lambda: not presenter._futures)
    assert presenter._score_generation == generation
    with _database.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(UpgradeDecisionRecord))
            == decision_count
        )
    assert presenter.shutdown()
    assert live.shutdown_count == 1

    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "wuwa_assistant"
    )
    assert "capture_session_create_requested monitor_index=1" in messages
    assert "capture_session_created capture_id=" in messages
    assert (
        "recognition_persistence_requested capture_id=" in messages
        and "packet_kind=content page_type=echo_enhancement relevant=True confidence=0.96000"
        in messages
    )
    assert "recognition_persisted capture_id=" in messages
    assert "reason=semantic_unchanged" in messages
    assert "reason=structurally_empty" in messages
    assert "session_created=True observation_created=True event_count=1" in messages
    assert "capture_session_stop_requested active=True" in messages
    assert "capture_session_stopped capture_id=" in messages
    for forbidden in (
        "不熄獵手",
        "人工確認名稱",
        "暴擊",
        "state-5",
        "semantic_fingerprint",
        "raw_ocr",
    ):
        assert forbidden not in messages


def test_presenter_rejects_incompatible_monitor_before_capture(
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = history_storage
    live = _FakeLiveController()
    echo_page = EchoLivePage()
    presenter = EchoWorkspacePresenter(
        live_controller=live,  # type: ignore[arg-type]
        history_service=service,
        echo_page=echo_page,
        history_page=EchoHistoryPage(),
        app_version="0.4.0",
    )
    incompatible = MonitorDescriptor(
        identifier="monitor-2",
        index=2,
        label="不相容顯示器",
        left=0,
        top=0,
        width=1920,
        height=1080,
        is_primary=False,
        is_compatible=False,
    )
    live.monitors_changed.emit((incompatible,))
    qtbot.waitUntil(lambda: echo_page.monitor_combo.currentData() == "monitor-2")
    presenter.start_capture("monitor-2", "", "")

    assert live.started_with is None
    assert "2560x1440" in echo_page.capture_detail_label.text()
    assert presenter.shutdown()


def test_presenter_applies_persisted_monitor_after_async_discovery(
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = history_storage
    live = _FakeLiveController()
    echo_page = EchoLivePage()
    presenter = EchoWorkspacePresenter(
        live_controller=live,  # type: ignore[arg-type]
        history_service=service,
        echo_page=echo_page,
        history_page=EchoHistoryPage(),
        preferred_monitor_id="monitor-2",
        app_version="0.9.0",
    )
    monitors = (
        MonitorDescriptor("monitor-1", 1, "顯示器 1", 0, 0, 2560, 1440, True, True),
        MonitorDescriptor("monitor-2", 2, "顯示器 2", 2560, 0, 2560, 1440, False, True),
    )

    live.monitors_changed.emit(monitors)
    qtbot.waitUntil(lambda: echo_page.monitor_combo.currentData() == "monitor-2")

    assert presenter.shutdown()


def test_runtime_error_stops_capture_and_allows_a_clean_restart(
    qtbot: QtBot,
    history_storage: tuple[Database, EchoHistoryService],
) -> None:
    _database, service = history_storage
    live = _FakeLiveController()
    echo_page = EchoLivePage()
    presenter = EchoWorkspacePresenter(
        live_controller=live,  # type: ignore[arg-type]
        history_service=service,
        echo_page=echo_page,
        history_page=EchoHistoryPage(),
        app_version="1.0.2",
    )
    presenter.initialize()
    qtbot.waitUntil(lambda: echo_page.monitor_combo.currentData() == "monitor-1")

    presenter.start_capture("monitor-1", "", "")
    qtbot.waitUntil(lambda: live.start_count == 1 and presenter._active_capture_id is not None)
    live.error_occurred.emit(
        CaptureControllerError(
            code=ControllerErrorCode.CAPTURE_FAILED,
            title="辨識工作發生錯誤",
            message="測試錯誤",
            error_type="RuntimeError",
            recoverable=True,
        )
    )

    qtbot.waitUntil(lambda: live.stop_count == 1)
    qtbot.waitUntil(lambda: presenter._active_capture_id is None and not presenter._futures)
    assert echo_page.start_button.isEnabled()
    assert not echo_page.stop_button.isEnabled()
    assert "測試錯誤" in echo_page.capture_detail_label.text()

    presenter.start_capture("monitor-1", "", "")
    qtbot.waitUntil(lambda: live.start_count == 2 and presenter._active_capture_id is not None)

    presenter.stop_capture()
    qtbot.waitUntil(lambda: live.stop_count == 2)
    qtbot.waitUntil(lambda: not presenter._futures)
    assert presenter.shutdown()


@pytest.mark.parametrize("pending", [False, True])
def test_switching_mode_cancels_old_capture_without_starting_new_one(
    qtbot: QtBot, history_storage: tuple[Database, EchoHistoryService], monkeypatch, pending: bool
) -> None:
    from threading import Event

    _database, service = history_storage
    live, page = _FakeLiveController(), EchoLivePage()
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=service,
        echo_page=page,
        history_page=EchoHistoryPage(),
        app_version="test",
    )
    gate, entered = Event(), Event()
    original = service.start_capture

    def delayed_start(**kwargs):
        entered.set()
        assert gate.wait(3)
        return original(**kwargs)

    if pending:
        monkeypatch.setattr(service, "start_capture", delayed_start)
    else:
        gate.set()
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: page.monitor_combo.currentData() == "monitor-1")
        page.select_capture_mode("manual")
        page.start_button.click()
        if pending:
            qtbot.waitUntil(entered.is_set)
        else:
            qtbot.waitUntil(lambda: live.started_with is not None)
            assert live.started_with.manual_mode
            live.status_changed.emit(CaptureStatusView(CaptureStatus.WAITING_FOR_REQUEST, "待命"))
            qtbot.waitUntil(lambda: page.start_button.isEnabled())
            page.start_button.click()
            assert live.recheck_count == 1 and live.start_count == 1
        page.capture_mode_combo.setCurrentIndex(page.capture_mode_combo.findData("auto"))
        gate.set()
        qtbot.waitUntil(lambda: not presenter._futures)
        qtbot.wait(20)
        assert presenter._active_capture_id is None
        assert live.start_count == (0 if pending else 1)
        assert page.start_button.isEnabled()
        assert page.start_button.text() == "開始辨識"
    finally:
        gate.set()
        presenter.shutdown()


def test_stop_failure_releases_presenter_gate_without_recursive_stop(
    qtbot: QtBot, history_storage: tuple[Database, EchoHistoryService]
) -> None:
    _database, service = history_storage

    class PendingStopController(_FakeLiveController):
        def stop(self) -> bool:
            self.stop_count += 1
            self.status_changed.emit(CaptureStatusView(CaptureStatus.STOPPING, "停止中"))
            return True

    live, page = PendingStopController(), EchoLivePage()
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=service,
        echo_page=page,
        history_page=EchoHistoryPage(),
        app_version="test",
    )
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: page.start_button.isEnabled())
        page.start_button.click()
        qtbot.waitUntil(lambda: live.start_count == 1)
        page.stop_button.click()
        assert presenter._capture_stopping
        live.error_occurred.emit(
            CaptureControllerError(
                ControllerErrorCode.STOP_FAILED,
                "停止逾時",
                "原生 OCR 尚未結束",
                "TimeoutError",
                True,
            )
        )
        qtbot.waitUntil(lambda: not presenter._capture_stopping)
        assert live.stop_count == 1
        assert page.start_button.isEnabled()
        page.start_button.click()
        qtbot.waitUntil(lambda: live.start_count == 2)
    finally:
        presenter.shutdown()
