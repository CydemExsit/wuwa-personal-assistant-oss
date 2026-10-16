from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QDialog
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.onboarding import OnboardingMode, OnboardingService
from wuwa_assistant.capture import FakeCaptureBackend, FrameLeaseAudit, MonitorInfo
from wuwa_assistant.display import DISPLAY_MODE_KEY, DISPLAY_SETUP_KEY, RESOLUTION_KEY
from wuwa_assistant.models import OcrToken
from wuwa_assistant.ocr import FakeOcrEngine, OcrBackendError
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.ui.controllers.onboarding import (
    OnboardingController,
    _monitor_choices,
)
from wuwa_assistant.ui.pages.onboarding import CaptureTestRequest, OnboardingWizard
from wuwa_assistant.vision.rois import REFERENCE_HEIGHT, REFERENCE_WIDTH


@pytest.mark.parametrize("resolution", ["1920x1080", "2560x1440"])
def test_controller_discovers_tests_releases_and_quick_starts_without_frame_write(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    resolution: str,
) -> None:
    paths = AppPaths.from_root(tmp_path / "local-data")
    database = create_database(paths)
    audits: list[FrameLeaseAudit] = []
    width, height = (int(value) for value in resolution.split("x"))

    def backend_factory() -> FakeCaptureBackend:
        audit = FrameLeaseAudit()
        audits.append(audit)
        frame = np.full((height, width, 3), 48, dtype=np.uint8)
        frame[:, ::2] = 144
        return FakeCaptureBackend(
            (frame,),
            monitors=(MonitorInfo(2, 2560, 0, width, height, False, "Monitor 2"),),
            audit=audit,
        )

    wizard = OnboardingWizard(data_path=paths.root)
    wizard.display_mode_combo.setCurrentIndex(wizard.display_mode_combo.findData("single"))
    wizard.monitor_page.resolution_combo.setCurrentIndex(
        wizard.monitor_page.resolution_combo.findData(resolution)
    )
    service = OnboardingService(
        database.session_factory,
        database.settings,
        data_path=paths.root,
    )
    controller = OnboardingController(
        service=service,
        wizard=wizard,
        backend_factory=backend_factory,
        ocr_factory=FakeOcrEngine,
        parent=wizard,
    )
    try:
        controller.initialize()
        qtbot.waitUntil(lambda: wizard.monitor_page.combo.currentData() == "monitor-2")
        assert "唯一螢幕" in wizard.monitor_page.combo.currentText()
        before = {item.relative_to(paths.root) for item in paths.root.rglob("*")}

        controller.test_capture(CaptureTestRequest("monitor-2", resolution))
        qtbot.waitUntil(wizard.monitor_page.isComplete)

        after = {item.relative_to(paths.root) for item in paths.root.rglob("*")}
        assert after == before
        assert len(audits) == 2
        assert audits[1].snapshot.acquired == 1
        assert audits[1].snapshot.released == 1
        assert audits[1].snapshot.active == 0
        assert "未看到鳴潮聲骸頁面" in wizard.monitor_page.status_label.text()
        assert "沒有寫入硬碟" in wizard.monitor_page.status_label.text()

        wizard.accept()
        qtbot.waitUntil(lambda: wizard.result() == QDialog.DialogCode.Accepted)

        state = service.load_state()
        assert state.completed
        assert state.monitor_identifier == "monitor-2"
        assert state.mode is OnboardingMode.QUICK_START
        assert state.character_id is None
        assert database.settings.get(DISPLAY_MODE_KEY) == "single"
        assert database.settings.get(RESOLUTION_KEY) == resolution
        assert database.settings.get(DISPLAY_SETUP_KEY) is True
    finally:
        controller.shutdown()
        wizard.close()
        qapp.processEvents()
        database.dispose()


def test_controller_rejects_black_or_detail_free_capture(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "black-capture")
    database = create_database(paths)
    frame = np.zeros((REFERENCE_HEIGHT, REFERENCE_WIDTH, 3), dtype=np.uint8)

    def backend_factory() -> FakeCaptureBackend:
        return FakeCaptureBackend(
            (frame,),
            monitors=(MonitorInfo(1, 0, 0, 2560, 1440, True, "Primary"),),
        )

    wizard = OnboardingWizard(data_path=paths.root)
    controller = OnboardingController(
        service=OnboardingService(
            database.session_factory,
            database.settings,
            data_path=paths.root,
        ),
        wizard=wizard,
        backend_factory=backend_factory,
        ocr_factory=FakeOcrEngine,
        parent=wizard,
    )
    try:
        controller.initialize()
        qtbot.waitUntil(lambda: wizard.monitor_page.combo.currentData() == "monitor-1")
        controller.test_capture(CaptureTestRequest("monitor-1"))
        qtbot.waitUntil(lambda: "沒有畫面細節" in wizard.monitor_page.status_label.text())

        assert not wizard.monitor_page.isComplete()
        assert wizard.monitor_page.status_label.property("captureTest") == "error"
        assert "改選另一個螢幕" in wizard.monitor_page.status_label.text()
        assert "沒有寫入硬碟" in wizard.monitor_page.status_label.text()
    finally:
        controller.shutdown()
        wizard.close()
        qapp.processEvents()
        database.dispose()


def test_monitor_labels_explain_topology_primary_and_windows_index() -> None:
    choices = _monitor_choices(
        (
            MonitorInfo(1, 0, 0, 2560, 1440, True, "Display A"),
            MonitorInfo(2, -2560, 0, 2560, 1440, False, "Display B"),
        )
    )

    assert choices[0].identifier == "monitor-1"
    assert "右側螢幕" in choices[0].label
    assert "Windows 顯示器 1" in choices[0].label
    assert "主要螢幕" in choices[0].label
    assert "左側螢幕" in choices[1].label
    assert "Windows 顯示器 2" in choices[1].label


def test_controller_reports_real_page_classifier_result_without_saving_frame(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "page-detection")
    database = create_database(paths)
    audit = FrameLeaseAudit()
    frame = np.full((REFERENCE_HEIGHT, REFERENCE_WIDTH, 3), 48, dtype=np.uint8)
    frame[:, ::2] = 144

    def backend_factory() -> FakeCaptureBackend:
        return FakeCaptureBackend(
            (frame,),
            monitors=(MonitorInfo(1, 0, 0, 2560, 1440, True, "Primary"),),
            audit=audit,
        )

    def ocr_factory() -> FakeOcrEngine:
        return FakeOcrEngine(
            (
                (OcrToken("聲骸強化", 0.97, (0, 0, 100, 40)),),
                (),
                (),
            )
        )

    wizard = OnboardingWizard(data_path=paths.root)
    controller = OnboardingController(
        service=OnboardingService(
            database.session_factory,
            database.settings,
            data_path=paths.root,
        ),
        wizard=wizard,
        backend_factory=backend_factory,
        ocr_factory=ocr_factory,
        parent=wizard,
    )
    try:
        controller.initialize()
        qtbot.waitUntil(lambda: wizard.monitor_page.combo.currentData() == "monitor-1")
        before = {item.relative_to(paths.root) for item in paths.root.rglob("*")}
        controller.test_capture(CaptureTestRequest("monitor-1"))
        qtbot.waitUntil(wizard.monitor_page.isComplete)

        message = wizard.monitor_page.status_label.text()
        assert "辨識為「聲骸強化」" in message
        assert "97%" in message
        assert "沒有寫入硬碟" in message
        assert {item.relative_to(paths.root) for item in paths.root.rglob("*")} == before
        audit.assert_all_released()
    finally:
        controller.shutdown()
        wizard.close()
        qapp.processEvents()
        database.dispose()


def test_controller_explains_missing_local_ocr_model(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "missing-ocr")
    database = create_database(paths)
    frame = np.full((REFERENCE_HEIGHT, REFERENCE_WIDTH, 3), 96, dtype=np.uint8)

    def backend_factory() -> FakeCaptureBackend:
        return FakeCaptureBackend(
            (frame,),
            monitors=(MonitorInfo(1, 0, 0, 2560, 1440, True, "Primary"),),
        )

    def missing_ocr() -> FakeOcrEngine:
        raise OcrBackendError("model files missing")

    wizard = OnboardingWizard(data_path=paths.root)
    controller = OnboardingController(
        service=OnboardingService(
            database.session_factory,
            database.settings,
            data_path=paths.root,
        ),
        wizard=wizard,
        backend_factory=backend_factory,
        ocr_factory=missing_ocr,
        parent=wizard,
    )
    try:
        controller.initialize()
        qtbot.waitUntil(lambda: wizard.monitor_page.combo.currentData() == "monitor-1")
        controller.test_capture(CaptureTestRequest("monitor-1"))
        qtbot.waitUntil(lambda: "本機 OCR 模型無法使用" in wizard.monitor_page.status_label.text())

        assert not wizard.monitor_page.isComplete()
        assert "重新安裝完整封裝" in wizard.monitor_page.status_label.text()
    finally:
        controller.shutdown()
        wizard.close()
        qapp.processEvents()
        database.dispose()
