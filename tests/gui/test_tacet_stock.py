from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.characters import CharacterCultivationService
from wuwa_assistant.application.farming import TodayFarmingSnapshot
from wuwa_assistant.application.planning import CultivationPlanningService
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.application.tacet_catalog import load_tacet_catalog
from wuwa_assistant.application.tacet_stock import TacetStockService
from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmRole,
    FarmZone,
    recommend_zones,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.ui.controllers.today_plan import TodayPlanPresenter
from wuwa_assistant.ui.pages.today_plan import TacetStockEditRequest, TodayPlanPage


def stock_snapshot(stocked_field_ids: frozenset[str] = frozenset()) -> TodayFarmingSnapshot:
    catalog = load_tacet_catalog()
    chosen = (
        catalog.fields[0],
        next(field for field in catalog.fields if "高天共奏之曲" in field.set_names),
    )
    zones = tuple(FarmZone(f.field_id, f.name, f.region, f.set_names) for f in chosen)
    roster = tuple(
        FarmCharacter(
            str(index),
            f"測試角色{index}",
            FarmRole.MAIN,
            "主輸出",
            (field.set_names[0],),
            (FarmEcho(1, 3, 25, 10 + index * 40, field.set_names[0], True, True),),
            priority=index + 1,
        )
        for index, field in enumerate(chosen)
    )
    return TodayFarmingSnapshot(
        (), recommend_zones(roster, zones, stocked_field_ids=stocked_field_ids), "測試來源", 2
    )


def test_stock_controls_emit_stable_field_ids_after_reordering_and_disable_while_saving(
    qtbot: QtBot,
) -> None:
    page = TodayPlanPage()
    qtbot.addWidget(page)
    requests: list[TacetStockEditRequest] = []
    page.tacet_stock_edit_requested.connect(requests.append)
    snapshot = stock_snapshot()
    field_id = snapshot.farming.recommendations[0].zone.identifier
    page.set_farming(snapshot)
    button = page.findChild(QPushButton, f"TodayPlanStock_{field_id}")
    assert button is not None
    button.click()
    assert requests == [TacetStockEditRequest((field_id,), True)]
    page.set_farming(stock_snapshot(frozenset({field_id})))
    assert "高天" in str(page.zone_model.data(page.zone_model.index(0, 1)))
    # Click by the stable field identity after rows move.
    button = page.zone_table.indexWidget(page.zone_model.index(1, 3))
    assert isinstance(button, QPushButton)
    button.click()
    assert requests[-1] == TacetStockEditRequest((field_id,), False)
    page.set_loading(True)
    assert not button.isEnabled()
    assert not page.stock_all_available.isEnabled()
    page.set_loading(False)
    page.stock_all_available.click()
    assert set(requests[-1].field_ids) == set(page._field_ids)
    assert requests[-1].has_stock
    page.set_farming(stock_snapshot(frozenset(page._field_ids)))
    assert "先練現有聲骸" in page.zone_summary_label.text()
    assert page.zone_model.rowCount() == 2


def test_stock_presenter_persists_single_click_bulk_restart_and_dashboard(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database(AppPaths.from_root(tmp_path))
    page = TodayPlanPage()
    qtbot.addWidget(page)
    presenter: TodayPlanPresenter | None = None

    def build(
        *args: object, stocked_field_ids: frozenset[str], **kwargs: object
    ) -> TodayFarmingSnapshot:
        return stock_snapshot(stocked_field_ids)

    monkeypatch.setattr("wuwa_assistant.ui.controllers.today_plan.build_today_farming", build)
    updates: list[str] = []
    field_id = stock_snapshot().farming.recommendations[0].zone.identifier

    def create_presenter() -> TodayPlanPresenter:
        result = TodayPlanPresenter(
            planning_service=CultivationPlanningService(database.session_factory),
            character_service=CharacterCultivationService(database.session_factory),
            scoring_profiles=ScoringProfileService(database.session_factory),
            settings=database.settings,
            page=page,
            data_set_id="primary",
            today=lambda: date(2026, 10, 22),
        )
        result.dashboard_priority_changed.connect(updates.append)
        return result

    try:
        presenter = create_presenter()
        presenter.initialize()
        qtbot.waitUntil(lambda: page.zone_model.rowCount() == 2)
        assert "高天" in str(page.zone_model.data(page.zone_model.index(0, 1)))
        initial_skills = page.skill_model.rowCount()
        button = page.findChild(QPushButton, f"TodayPlanStock_{field_id}")
        assert button is not None
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: "狀態已保存" in page.status_label.text())
        assert field_id not in TacetStockService(database.settings).stocked_field_ids(
            "primary", load_tacet_catalog()
        )
        assert page.zone_model.data(page.zone_model.index(0, 0)) == "1. 中曲臺地無音區"
        assert "中曲臺地" in updates[-1]
        page.stock_all_available.click()
        qtbot.waitUntil(lambda: "先培養現有庫存" in updates[-1])
        assert "先練現有聲骸" in page.zone_summary_label.text()
        assert page.skill_model.rowCount() == initial_skills
        assert presenter.shutdown()
        page = TodayPlanPage()
        qtbot.addWidget(page)
        presenter = create_presenter()
        presenter.initialize()
        qtbot.waitUntil(lambda: page.zone_model.rowCount() == 2)
        assert "先練現有聲骸" in page.zone_summary_label.text()
        button = page.findChild(QPushButton, f"TodayPlanStock_{field_id}")
        assert button is not None
        button.click()
        qtbot.waitUntil(lambda: "狀態已保存" in page.status_label.text())
        assert "中曲臺地" in updates[-1]
        with monkeypatch.context() as patch:

            def cannot_save(*args: object, **kwargs: object) -> None:
                raise OSError("simulated unavailable storage")

            patch.setattr(TacetStockService, "save", cannot_save)
            button = page.findChild(QPushButton, f"TodayPlanStock_{field_id}")
            assert button is not None
            button.click()
            qtbot.waitUntil(lambda: "simulated unavailable storage" in page.status_label.text())
            assert button.isEnabled()
            assert button.text() == "需要的\n已用完"
            assert field_id not in TacetStockService(database.settings).stocked_field_ids(
                "primary", load_tacet_catalog()
            )
        assert presenter.shutdown()
    finally:
        if presenter is not None:
            presenter.shutdown()
        page.close()
        database.dispose()
