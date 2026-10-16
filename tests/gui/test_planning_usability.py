from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTime
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QPushButton

from tests.gui.helpers import MemorySettings
from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    SaveCharacter,
    UpdateCharacter,
    UpdateSkill,
)
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.farming import build_today_farming
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.public_data.models import OfficialNotice, OfficialSchedule
from wuwa_assistant.public_data.recurring import challenge_periods
from wuwa_assistant.public_data.schedule import ASIA_ZONE
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.characters import SkillProgressTableModel, SkillProgressView
from wuwa_assistant.ui.pages.settings import SettingsPage
from wuwa_assistant.ui.pages.today_plan import TodayPlanPage
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector
from wuwa_assistant.ui.widgets.official_feed import EventCountdownWidget
from wuwa_assistant.ui.window_state import LAST_PAGE_KEY


def test_two_choices_are_direct_buttons_and_survive_option_updates(qapp: QApplication) -> None:
    choice = ChoiceSelector()
    choice.addItem("角色總分", "characters")
    choice.addItem("低分單件", "pieces")
    choice.resize(320, 40)
    choice.show()
    qapp.processEvents()
    buttons = choice.findChildren(QPushButton, "ChoiceSegment")
    assert len(buttons) == 2 and all(button.isVisible() for button in buttons)
    spy = QSignalSpy(choice.currentIndexChanged)
    QTest.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    assert choice.currentData() == "pieces" and spy.count() == 1
    choice.blockSignals(True)
    choice.setCurrentIndex(0)
    choice.blockSignals(False)
    assert buttons[0].isChecked() and not buttons[1].isChecked()
    choice.addItem("第三項", "third")
    assert all(button.isHidden() for button in buttons)
    choice.removeItem(2)
    assert all(button.isVisible() for button in buttons)
    choice.close()


def test_time_picker_emits_hour_and_minute(qapp: QApplication) -> None:
    page = SettingsPage(MemorySettings({"reminders/weekend_hour": 10}))
    assert page.reminder_time_edit.time() == QTime(10, 0)
    spy = QSignalSpy(page.background_reminders_requested)
    page.reminder_time_edit.setTime(QTime(18, 45))
    page.background_reminders_check.setChecked(True)
    page.reminder_apply_button.click()
    assert spy.at(0) == [True, 18, 45]
    page.close()


def test_old_history_navigation_restores_inside_settings(qapp: QApplication) -> None:
    window = MainWindow(MemorySettings({LAST_PAGE_KEY: "history"}))
    window.show()
    qapp.processEvents()
    assert window.current_page_id is PageId.SETTINGS
    assert window.settings_tabs.currentWidget() is window.history_page
    assert "歷史紀錄" not in window.navigation_model.labels()
    assert window.history_page.isVisible()
    window.close()


def test_planning_has_no_date_control_and_uses_current_data(qapp: QApplication) -> None:
    page = TodayPlanPage()
    page.show()
    qapp.processEvents()
    assert page.plan_date_edit.isHidden()
    assert page.current_request().plan_date == date.today()
    assert page.skill_group.isVisible() and page.zone_group.isVisible()
    page.close()


def test_skill_priority_reorder_persists_across_database_reopen(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)
    with create_database(paths) as database:
        dataset = EchoHistoryService(database.session_factory).ensure_active_primary_dataset()
        service = CharacterCultivationService(database.session_factory)
        character = service.create_character(
            data_set_id=dataset, command=CreateCharacter(name="心")
        )
        ordered = tuple(reversed(character.skills))
        saved = service.save_character(
            data_set_id=dataset,
            character_id=character.summary.id,
            command=SaveCharacter(
                character=UpdateCharacter(
                    name="心",
                    element=None,
                    role=None,
                    is_candidate=True,
                    priority=100,
                    level=1,
                    resonance_chain=0,
                    scoring_profile_id=None,
                    notes=None,
                    cultivation_goal={"skill_order_custom": True},
                ),
                skills=tuple(
                    UpdateSkill(
                        skill.skill_key,
                        skill.name,
                        skill.skill_type,
                        skill.current_level,
                        skill.target_level,
                        skill.target_level >= 9,
                        index,
                    )
                    for index, skill in enumerate(ordered)
                ),
            ),
        )
        assert [skill.skill_key for skill in saved.skills] == [skill.skill_key for skill in ordered]
    with create_database(paths) as database:
        reloaded = CharacterCultivationService(database.session_factory).get_character(
            character.summary.id, data_set_id=dataset
        )
        assert [skill.skill_key for skill in reloaded.skills] == [
            skill.skill_key for skill in ordered
        ]
        profiles = ScoringProfileService(database.session_factory)
        profiles.ensure_system_templates(load_template_seed_definitions())
        plan = build_today_farming(
            CharacterCultivationService(database.session_factory), profiles, data_set_id=dataset
        )
        assert [skill.skill_key for skill in plan.skills] == [skill.skill_key for skill in ordered]


def test_skill_order_buttons_have_no_core_checkbox(qapp: QApplication) -> None:
    model = SkillProgressTableModel()
    model.set_rows(
        (SkillProgressView("a", "甲", 1, 6, False), SkillProgressView("b", "乙", 1, 10, True))
    )
    assert model.move_priority(1, -1)
    assert [row.skill_key for row in model.rows()] == ["b", "a"]
    assert not model.move_priority(0, -1)
    assert model.data(model.index(0, 3)) == 1
    assert model.data(model.index(0, 3), Qt.ItemDataRole.CheckStateRole) is None


def test_events_compact_deadline_order_and_future_collapsed(qapp: QApplication) -> None:
    now = datetime(2026, 10, 10, 10, tzinfo=ASIA_ZONE)
    a = OfficialSchedule(
        "a",
        "較晚活動",
        "event",
        "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/1",
        now - timedelta(days=1),
        now + timedelta(days=8),
    )
    b = replace(a, key="b", title="即將到期", ends_at=now + timedelta(days=2))
    future = replace(a, key="future", starts_at=now + timedelta(days=1))
    notice = OfficialNotice(
        1,
        "公告",
        "event",
        a.official_url,
        None,
        None,
        None,
        "needs_manual_confirmation",
        (a, b, future),
    )
    page = EventCountdownWidget()
    page.timer.stop()
    page.resize(900, 700)
    page.set_notices((notice,))
    page.show()
    qapp.processEvents()
    page.refresh(now)
    assert page.events_layout.count() == 2
    assert page.events_layout.itemAtPosition(0, 0).widget().findChild(QPushButton) is None
    assert page.events_layout.itemAtPosition(0, 1) is not None
    regular = [row[0].key for row in page._rows if not row[0].key.startswith("cycle:")]
    assert regular == ["b", "a", "future"]
    assert page.upcoming_box.isHidden()
    page.upcoming_button.click()
    assert page.upcoming_box.isVisible()
    tower, wastes = challenge_periods(now)
    page.set_completed(frozenset({tower.key, wastes.key}))
    page.refresh(datetime(2026, 10, 12, 4, tzinfo=ASIA_ZONE))
    cycles = {
        row[0].key: row[1].isChecked() for row in page._rows if row[0].key.startswith("cycle:")
    }
    assert cycles[wastes.key] is True
    assert tower.key not in cycles and cycles["cycle:tower:2026-10-12"] is False
    page.close()
