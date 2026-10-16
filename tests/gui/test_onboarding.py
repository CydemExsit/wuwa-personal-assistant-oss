from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel

from wuwa_assistant.ui.pages.onboarding import (
    CaptureTestRequest,
    MonitorChoice,
    OnboardingRequest,
    OnboardingWizard,
)


def _monitors() -> tuple[MonitorChoice, ...]:
    return (
        MonitorChoice("monitor-1", "左側螢幕 (Windows 顯示器 1) · 2560x1440"),
        MonitorChoice(
            "monitor-2",
            "右側螢幕 (Windows 顯示器 2) · 主要螢幕 · 2560x1440",
        ),
    )


def test_short_wizard_has_three_pages_and_non_gate_safety_explanation(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    wizard = OnboardingWizard(data_path=tmp_path.resolve(), monitors=_monitors())
    wizard.show()
    qapp.processEvents()
    try:
        assert len(wizard.pageIds()) == 3
        safety = wizard.findChild(QLabel, "OnboardingSafetyText")
        assert safety is not None
        assert "不操作遊戲" in safety.text()
        assert "不讀取遊戲記憶體" in safety.text()
        assert "獨立小面板" in safety.text()
        assert wizard.findChildren(QCheckBox) == []
        assert "解析度" in wizard.welcome_page.subTitle()
        assert "切換" in wizard.welcome_page.subTitle()
    finally:
        wizard.close()


def test_wizard_emits_quick_start_completion_without_writing_frames(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "local-app-data"
    wizard = OnboardingWizard(data_path=data_path, monitors=_monitors())
    capture_spy = QSignalSpy(wizard.capture_test_requested)
    completion_spy = QSignalSpy(wizard.completion_requested)
    wizard.show()
    qapp.processEvents()
    try:
        wizard.next()
        QTest.mouseClick(wizard.monitor_page.test_button, Qt.MouseButton.LeftButton)
        assert capture_spy.count() == 1
        assert capture_spy.at(0)[0] == CaptureTestRequest("monitor-1")
        assert not data_path.exists()

        wizard.set_capture_test_result("monitor-1", True, "辨識為聲骸強化")
        assert wizard.monitor_page.isComplete()
        wizard.next()
        assert wizard.currentPage() is wizard.summary_page
        assert "左側螢幕" in wizard.summary_label.text()

        wizard.accept()
        assert completion_spy.count() == 1
        request = completion_spy.at(0)[0]
        assert isinstance(request, OnboardingRequest)
        assert request.monitor_identifier == "monitor-1"
        assert request.data_path == data_path.resolve()
        assert request.initial_mode == "quick_start"
        assert request.character_name is None
        assert request.scoring_template_kind == "generic_dps"
        assert request.safety_confirmed
        assert request.capture_test_confirmed
        assert request.data_path_confirmed
        assert wizard.result() != QDialog.DialogCode.Accepted
        assert not data_path.exists()

        wizard.complete_success()
        assert wizard.result() == QDialog.DialogCode.Accepted
        assert not data_path.exists()
    finally:
        wizard.close()


def test_monitor_change_invalidates_test_and_stale_result_is_ignored(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    wizard = OnboardingWizard(data_path=tmp_path.resolve(), monitors=_monitors())
    wizard.show()
    qapp.processEvents()
    try:
        wizard.set_capture_test_result("monitor-1", True, "成功")
        assert wizard.monitor_page.isComplete()

        wizard.monitor_page.combo.setCurrentIndex(1)
        assert not wizard.monitor_page.isComplete()
        assert "測試這個螢幕" in wizard.monitor_page.status_label.text()

        wizard.set_capture_test_result("monitor-1", True, "過時結果")
        assert not wizard.monitor_page.isComplete()
        assert "過時結果" not in wizard.monitor_page.status_label.text()
        wizard.set_capture_test_result("monitor-2", False, "解析度不支援")
        assert not wizard.monitor_page.isComplete()
        assert wizard.monitor_page.status_label.text() == "解析度不支援"
    finally:
        wizard.close()


def test_completion_requires_only_successful_capture_test(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    wizard = OnboardingWizard(data_path=tmp_path.resolve(), monitors=_monitors())
    completion_spy = QSignalSpy(wizard.completion_requested)
    wizard.show()
    qapp.processEvents()
    try:
        wizard.accept()
        assert completion_spy.count() == 0
        assert "測試" in wizard.completion_status_label.text()

        wizard.set_capture_test_result("monitor-1", True, "成功")
        wizard.accept()
        assert completion_spy.count() == 1
        request = completion_spy.at(0)[0]
        assert isinstance(request, OnboardingRequest)
        assert request.initial_mode == "quick_start"
        assert request.character_name is None
    finally:
        wizard.close()


def test_onboarding_request_rejects_unconfirmed_capture(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="測試"):
        OnboardingRequest(
            monitor_identifier="monitor-1",
            data_path=tmp_path.resolve(),
            capture_test_confirmed=False,
        )
