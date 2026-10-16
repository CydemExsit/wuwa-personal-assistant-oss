from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from tests.gui.helpers import MemorySettings
from wuwa_assistant import __version__
from wuwa_assistant.ui.pages.settings import (
    DEBUG_RETENTION_DAYS_KEY,
    EXCEPTIONAL_THRESHOLD_KEY,
    MONITOR_KEY,
    OCR_CONFIDENCE_KEY,
    PASS_THRESHOLD_KEY,
    PERFECT_THRESHOLD_KEY,
    SAMPLE_RATE_KEY,
    SAVE_LOW_CONFIDENCE_ROI_KEY,
    STABILITY_WAIT_KEY,
    MonitorOption,
    SettingsPage,
)


def test_settings_page_loads_and_saves_capture_and_scoring_values(
    qapp: QApplication,
) -> None:
    settings = MemorySettings(
        {
            MONITOR_KEY: "monitor-2",
            SAMPLE_RATE_KEY: 7.0,
            OCR_CONFIDENCE_KEY: 0.91,
            SAVE_LOW_CONFIDENCE_ROI_KEY: True,
        }
    )
    page = SettingsPage(
        settings,
        monitors=(
            MonitorOption("monitor-1", "顯示器 1 · 2560x1440"),
            MonitorOption("monitor-2", "顯示器 2 · 2560x1440"),
        ),
    )
    page.show()
    qapp.processEvents()

    assert page.monitor_combo.currentData() == "monitor-2"
    assert page.sample_rate_spin.value() == 7.0
    assert page.ocr_confidence_spin.value() == 0.91
    assert page.save_debug_check.isChecked()

    page.sample_rate_spin.setValue(4.5)
    page.stability_wait_spin.setValue(0.75)
    page.ocr_confidence_spin.setValue(0.88)
    page.retention_days_spin.setValue(21)
    page.pass_threshold_spin.setValue(72)
    page.perfect_threshold_spin.setValue(90)
    page.exceptional_threshold_spin.setValue(97)
    spy = QSignalSpy(page.settings_saved)
    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    assert spy.count() == 1
    assert settings.values[SAMPLE_RATE_KEY] == 4.5
    assert settings.values[STABILITY_WAIT_KEY] == 0.75
    assert settings.values[OCR_CONFIDENCE_KEY] == 0.88
    assert settings.values[DEBUG_RETENTION_DAYS_KEY] == 21
    assert settings.values[PASS_THRESHOLD_KEY] == 72
    assert settings.values[PERFECT_THRESHOLD_KEY] == 90
    assert settings.values[EXCEPTIONAL_THRESHOLD_KEY] == 97
    assert settings.sync_count == 1
    assert page.status_label.text() == "設定已儲存。"
    page.close()


def test_settings_page_rejects_out_of_order_score_thresholds(qapp: QApplication) -> None:
    settings = MemorySettings()
    page = SettingsPage(settings)
    page.show()
    qapp.processEvents()
    page.pass_threshold_spin.setValue(90)
    page.perfect_threshold_spin.setValue(80)
    page.exceptional_threshold_spin.setValue(95)
    spy = QSignalSpy(page.settings_saved)

    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    assert spy.count() == 0
    assert settings.values == {}
    assert "及格 < 完美 < 極品" in page.status_label.text()
    page.close()


def test_settings_page_exposes_explicit_local_data_actions(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    page = SettingsPage(MemorySettings(), data_root=tmp_path)
    backup_spy = QSignalSpy(page.backup_requested)
    export_spy = QSignalSpy(page.export_requested)
    import_spy = QSignalSpy(page.import_requested)
    diagnostics_spy = QSignalSpy(page.diagnostics_requested)
    page.show()
    qapp.processEvents()

    QTest.mouseClick(page.backup_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.export_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.import_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.diagnostics_button, Qt.MouseButton.LeftButton)

    assert backup_spy.count() == 1
    assert export_spy.count() == 1
    assert import_spy.count() == 1
    assert diagnostics_spy.count() == 1
    assert page.data_root_label.text() == str(tmp_path.resolve())
    assert __version__ in page.version_label.text()
    page.set_data_operation_status("匯出完成")
    assert page.status_label.text() == "匯出完成"
    page.close()


def test_late_monitor_discovery_restores_persisted_onboarding_choice() -> None:
    settings = MemorySettings({MONITOR_KEY: "monitor-2"})
    page = SettingsPage(settings)

    page.set_monitors(
        (
            MonitorOption("monitor-1", "顯示器 1"),
            MonitorOption("monitor-2", "顯示器 2"),
        )
    )

    assert page.monitor_combo.currentData() == "monitor-2"
    page.close()
