from __future__ import annotations

from datetime import date
from pathlib import Path
from threading import Event, Lock

from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.gacha import (
    GachaIncomeDefinition,
    GachaPlanningService,
    GachaStatusSnapshot,
    GachaTargetDefinition,
    GachaWorkspaceSnapshot,
)
from wuwa_assistant.gacha.models import (
    ForecastScenario,
    GachaPlan,
    GachaResources,
    ScenarioPlan,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord
from wuwa_assistant.ui.controllers.gacha import GachaPresenter
from wuwa_assistant.ui.pages.gacha import (
    GachaIncomeSaveRequest,
    GachaPage,
    GachaStatusSaveRequest,
    GachaTargetSaveRequest,
)


def test_presenter_persists_crud_and_uses_only_manual_requirements(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "gacha-presenter"))
    page = GachaPage()
    try:
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            data_set_id = data_set.id
        service = GachaPlanningService(database.session_factory)
        target = service.create_target(
            data_set_id=data_set_id,
            definition=GachaTargetDefinition(
                target_type="character",
                name="目標角色",
                target_date=date(2026, 9, 1),
                priority=1,
                scenario_json={
                    "required_pulls": {"conservative": 20, "optimistic": 10},
                    "source_status": "manual_unverified",
                },
                notes="原始備註",
            ),
        )
        income = service.create_income(
            data_set_id=data_set_id,
            definition=GachaIncomeDefinition(
                label="版本活動",
                expected_date=date(2026, 8, 20),
                astrite_amount=1_600,
                pull_amount=3,
                pull_type="character",
                scenario="general",
                recurrence_rule_json={"kind": "manual", "interval": 1},
            ),
        )
        presenter = GachaPresenter(
            service=service,
            page=page,
            data_set_id=data_set_id,
            today=lambda: date(2026, 8, 8),
        )

        presenter.initialize()
        qtbot.waitUntil(lambda: page.target_model.rowCount() == 1)
        assert page.income_model.rowCount() == 1
        assert page.target_model.data(page.target_model.index(0, 5)) == "20 / ? / 10"
        assert "缺少 typical 情境的人工需求抽數" in page.warning_label.text()
        assert "1 個目標資料不足" in page.projection_cards["typical"].value_label.text()

        page.status_save_requested.emit(GachaStatusSaveRequest(1_600, 5, 0, 79, 0, True, False))
        qtbot.waitUntil(
            lambda: (
                (stored := service.get_status(data_set_id=data_set_id)) is not None
                and stored.astrite == 1_600
            )
        )
        qtbot.waitUntil(lambda: "目前抽卡資源已保存" in page.status_label.text())

        page.target_save_requested.emit(
            GachaTargetSaveRequest(
                target.id,
                "character",
                "目標角色",
                1,
                "2.0",
                date(2026, 9, 1),
                1,
                "active",
                20,
                15,
                10,
                "更新備註",
            )
        )
        qtbot.waitUntil(
            lambda: service.get_target(target.id, data_set_id=data_set_id).status == "active"
        )
        qtbot.waitUntil(
            lambda: "已配置 15 抽" in page.projection_cards["typical"].value_label.text()
        )
        stored_target = service.get_target(target.id, data_set_id=data_set_id)
        assert stored_target.scenario_json["source_status"] == "manual_unverified"
        assert stored_target.scenario_json["required_pulls"] == {
            "conservative": 20,
            "typical": 15,
            "optimistic": 10,
        }
        assert stored_target.scenario_json.get("pity") is None

        page.income_save_requested.emit(
            GachaIncomeSaveRequest(
                income.id,
                "版本活動更新",
                date(2026, 8, 21),
                2_000,
                4,
                "character",
                "general",
                False,
                "人工收入",
            )
        )
        qtbot.waitUntil(
            lambda: service.get_income(income.id, data_set_id=data_set_id).label == "版本活動更新"
        )
        stored_income = service.get_income(income.id, data_set_id=data_set_id)
        assert stored_income.scenario == "general"
        assert stored_income.recurrence_rule_json == {"kind": "manual", "interval": 1}

        page.target_save_requested.emit(
            GachaTargetSaveRequest(
                None,
                "weapon",
                "目標武器",
                1,
                None,
                date(2026, 9, 10),
                2,
                "planned",
                80,
                60,
                40,
                "",
            )
        )
        qtbot.waitUntil(lambda: len(service.list_targets(data_set_id=data_set_id)) == 2)
        created_target = next(
            item for item in service.list_targets(data_set_id=data_set_id) if item.id != target.id
        )

        page.income_save_requested.emit(
            GachaIncomeSaveRequest(
                None,
                "每週收入",
                date(2026, 8, 25),
                320,
                1,
                "weapon",
                "conservative",
                False,
                "",
            )
        )
        qtbot.waitUntil(lambda: len(service.list_incomes(data_set_id=data_set_id)) == 2)
        created_income = next(
            item for item in service.list_incomes(data_set_id=data_set_id) if item.id != income.id
        )

        page.target_delete_requested.emit(created_target.id)
        qtbot.waitUntil(lambda: len(service.list_targets(data_set_id=data_set_id)) == 1)
        page.income_delete_requested.emit(created_income.id)
        qtbot.waitUntil(lambda: len(service.list_incomes(data_set_id=data_set_id)) == 1)
        page.refresh_requested.emit()
        qtbot.waitUntil(
            lambda: page.target_model.rowCount() == 1 and page.refresh_button.isEnabled()
        )

        assert presenter.shutdown()
    finally:
        page.close()
        database.dispose()


class _RecordingPage(GachaPage):
    def __init__(self) -> None:
        super().__init__()
        self.applied_astrite: list[int] = []

    def set_snapshot(self, snapshot: object) -> None:
        super().set_snapshot(snapshot)
        self.applied_astrite.append(self.astrite_spin.value())


class _DelayedWorkspaceService:
    def __init__(self) -> None:
        self.first_started = Event()
        self.release_first = Event()
        self._lock = Lock()
        self._calls = 0

    def load_workspace(
        self, *, data_set_id: str, as_of: date | None = None
    ) -> GachaWorkspaceSnapshot:
        del data_set_id
        assert as_of == date(2026, 8, 8)
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.first_started.set()
            assert self.release_first.wait(timeout=5)
        return _empty_workspace(call)


def test_presenter_ignores_stale_background_result(
    qapp: QApplication,
    qtbot: QtBot,
) -> None:
    del qapp
    service = _DelayedWorkspaceService()
    page = _RecordingPage()
    presenter = GachaPresenter(  # type: ignore[arg-type]
        service=service,
        page=page,
        data_set_id="data-set",
        today=lambda: date(2026, 8, 8),
    )
    try:
        presenter.initialize()
        assert service.first_started.wait(timeout=2)
        presenter.refresh()
        service.release_first.set()
        qtbot.waitUntil(lambda: page.applied_astrite == [2])
        assert presenter.shutdown()
    finally:
        service.release_first.set()
        page.close()


def _empty_workspace(astrite: int) -> GachaWorkspaceSnapshot:
    scenarios = tuple(
        ScenarioPlan(
            scenario=scenario,
            allocations=(),
            remaining_resources=GachaResources(astrite=astrite),
            included_income_ids=(),
        )
        for scenario in ForecastScenario
    )
    return GachaWorkspaceSnapshot(
        status=GachaStatusSnapshot(
            id="status",
            data_set_id="data-set",
            astrite=astrite,
            limited_character_pulls=0,
            limited_weapon_pulls=0,
            character_pity=0,
            weapon_pity=0,
            character_guaranteed=False,
            weapon_guaranteed=False,
            updated_at="2026-08-08T00:00:00Z",
        ),
        targets=(),
        incomes=(),
        plan=GachaPlan(scenarios=scenarios, warnings=()),
    )
