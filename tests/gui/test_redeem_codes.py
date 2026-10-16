from __future__ import annotations

from dataclasses import replace
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton
from pytestqt.qtbot import QtBot

from tests.gui.helpers import MemorySettings
from wuwa_assistant.public_data.redeem_models import COMMUNITY_URL, RedeemCode, RedeemSnapshot
from wuwa_assistant.ui.controllers.redeem_codes import (
    AUTO_KEY,
    COMPLETED_KEY,
    NOTIFIED_KEY,
    RedeemCodesPresenter,
)
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId


class Service:
    def __init__(self) -> None:
        self.calls = 0
        self.gate = Event()
        self.gate.set()
        self.result = RedeemSnapshot(
            (RedeemCode("TESTGIFT", COMMUNITY_URL, "listed"),),
            "2026-10-12T00:00:00+00:00",
            "已更新",
            True,
            ("TESTGIFT",),
        )

    def load_cached(self) -> RedeemSnapshot:
        return replace(self.result, fresh=False)

    def synchronize(self) -> RedeemSnapshot:
        self.calls += 1
        self.gate.wait(10)
        return self.result


def test_new_code_banner_copy_dedup_restart_and_navigation(
    qapp: QApplication, qtbot: QtBot, request: pytest.FixtureRequest
) -> None:
    settings = MemorySettings()
    window = MainWindow(settings)
    window.show()
    service = Service()
    presenter = RedeemCodesPresenter(service, window, settings)  # type: ignore[arg-type]
    request.addfinalizer(presenter.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: window.redeem_banner.isVisible())
    assert service.calls == 1
    assert settings.get(NOTIFIED_KEY) == ["TESTGIFT"]
    window.redeem_view_button.click()
    assert window.current_page_id is PageId.REDEEM_CODES
    buttons = window.redeem_codes_page.findChildren(QPushButton)
    button = next(b for b in buttons if b.property("redeemCode") == "TESTGIFT")
    button.click()
    assert qapp.clipboard().text() == "TESTGIFT"
    assert "已複製 TESTGIFT" in window.redeem_codes_page.copy_status.text()
    window.redeem_dismiss_button.click()
    presenter.refresh()
    qtbot.waitUntil(lambda: service.calls == 2 and not presenter._busy)
    assert not window.redeem_banner.isVisible()
    presenter.shutdown()
    second = RedeemCodesPresenter(service, window, settings)  # type: ignore[arg-type]
    request.addfinalizer(second.shutdown)
    second.initialize()
    qtbot.waitUntil(lambda: service.calls == 3 and not second._busy)
    assert not window.redeem_banner.isVisible()


def test_disabled_auto_cache_failure_and_shutdown_do_not_notify(
    qapp: QApplication, qtbot: QtBot, request: pytest.FixtureRequest
) -> None:
    settings = MemorySettings({AUTO_KEY: False})
    window = MainWindow(settings)
    window.show()
    service = Service()
    presenter = RedeemCodesPresenter(service, window, settings)  # type: ignore[arg-type]
    request.addfinalizer(presenter.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: not presenter._busy)
    assert service.calls == 0
    assert not window.redeem_banner.isVisible()
    service.result = replace(service.result, fresh=False, message="更新失敗")
    presenter.refresh()
    qtbot.waitUntil(lambda: service.calls == 1 and not presenter._busy)
    assert not window.redeem_banner.isVisible()
    service.gate.clear()
    service.result = replace(service.result, fresh=True)
    presenter.refresh()
    presenter.refresh()
    qtbot.waitUntil(lambda: service.calls == 2)
    presenter.prepare_shutdown()
    service.gate.set()
    presenter.finish_shutdown()
    qapp.processEvents()
    assert service.calls == 2 and not presenter._timer.isActive()
    assert not window.redeem_banner.isVisible()
    presenter.refresh()
    assert service.calls == 2


def test_expired_codes_hidden_and_copy_disabled(qapp: QApplication) -> None:
    window = MainWindow(MemorySettings())
    page = window.redeem_codes_page
    page.set_snapshot(RedeemSnapshot((RedeemCode("OLDGIFT", COMMUNITY_URL, "expired"),)))
    assert not page.code_scroll.widget().findChildren(QPushButton)
    page.show_old.setChecked(True)
    copy = next(
        b for b in page.code_scroll.widget().findChildren(QPushButton) if b.property("redeemCode")
    )
    assert not copy.isEnabled()


def test_session_mute_defers_pending_and_later_codes_until_restart(
    qapp: QApplication, qtbot: QtBot, request: pytest.FixtureRequest
) -> None:
    settings = MemorySettings()
    window = MainWindow(settings)
    window.show()
    service = Service()
    presenter = RedeemCodesPresenter(service, window, settings)  # type: ignore[arg-type]
    request.addfinalizer(presenter.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: window.redeem_banner.isVisible())
    window.redeem_mute_button.click()
    assert not window.redeem_banner.isVisible()
    assert settings.get(NOTIFIED_KEY) == []
    assert presenter._timer.isActive() and window.redeem_codes_page.auto_check.isChecked()
    new = RedeemCode("LATERGIFT", COMMUNITY_URL, "listed")
    service.result = replace(
        service.result, codes=(*service.result.codes, new), checked_codes=("TESTGIFT", new.key)
    )
    presenter.refresh()
    qtbot.waitUntil(lambda: service.calls == 2 and not presenter._busy)
    assert len(window.redeem_codes_page.code_scroll.widget().findChildren(QCheckBox)) == 2
    assert not window.redeem_banner.isVisible()
    assert settings.get(NOTIFIED_KEY) == []
    presenter.shutdown()
    window.close()
    second_window = MainWindow(settings)
    second_window.show()
    second = RedeemCodesPresenter(service, second_window, settings)  # type: ignore[arg-type]
    request.addfinalizer(second.shutdown)
    second.initialize()
    qtbot.waitUntil(lambda: second_window.redeem_banner.isVisible())
    assert "2 組" in second_window.redeem_notice.text()
    assert not second_window.redeem_codes_page.muted_label.isVisible()


def test_completed_codes_persist_hide_from_list_and_notification_and_can_be_undone(
    qapp: QApplication, qtbot: QtBot, request: pytest.FixtureRequest
) -> None:
    settings = MemorySettings()
    window = MainWindow(settings)
    window.show()
    service = Service()
    service.result = replace(
        service.result,
        codes=(*service.result.codes, RedeemCode("OLDGIFT", COMMUNITY_URL, "expired")),
    )
    presenter = RedeemCodesPresenter(service, window, settings)  # type: ignore[arg-type]
    request.addfinalizer(presenter.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: window.redeem_banner.isVisible())
    page = window.redeem_codes_page
    checkbox = page.code_scroll.widget().findChildren(QCheckBox)[0]
    checkbox.click()
    assert settings.get(COMPLETED_KEY) == ["TESTGIFT"]
    assert not page.code_scroll.widget().findChildren(QCheckBox)
    assert not window.redeem_banner.isVisible()
    presenter.refresh()
    qtbot.waitUntil(lambda: service.calls == 2 and not presenter._busy)
    assert not page.code_scroll.widget().findChildren(QCheckBox)
    presenter.shutdown()
    window.close()
    second_window = MainWindow(settings)
    second_window.show()
    second = RedeemCodesPresenter(service, second_window, settings)  # type: ignore[arg-type]
    request.addfinalizer(second.shutdown)
    second.initialize()
    qtbot.waitUntil(lambda: service.calls == 3 and not second._busy)
    page = second_window.redeem_codes_page
    assert not page.code_scroll.widget().findChildren(QCheckBox)
    assert not second_window.redeem_banner.isVisible()
    page.show_old.click()
    checks = page.code_scroll.widget().findChildren(QCheckBox)
    assert len(checks) == 2
    done = next(c for c in checks if c.property("redeemCode") == "TESTGIFT")
    assert done.isChecked()
    done.click()
    assert settings.get(COMPLETED_KEY) == []
    page.show_old.click()
    assert len(page.code_scroll.widget().findChildren(QCheckBox)) == 1


def test_completion_storage_failure_restores_checkbox_and_notification(
    qapp: QApplication, qtbot: QtBot, request: pytest.FixtureRequest
) -> None:
    class FailingSettings(MemorySettings):
        def set(self, key: str, value: object) -> None:
            if key == COMPLETED_KEY:
                raise OSError("read only")
            super().set(key, value)

    settings = FailingSettings()
    window = MainWindow(settings)
    window.show()
    presenter = RedeemCodesPresenter(Service(), window, settings)  # type: ignore[arg-type]
    request.addfinalizer(presenter.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: window.redeem_banner.isVisible())
    page = window.redeem_codes_page
    page.code_scroll.widget().findChildren(QCheckBox)[0].click()
    assert not page.code_scroll.widget().findChildren(QCheckBox)[0].isChecked()
    assert "儲存失敗" in page.status_label.text()
    assert window.redeem_banner.isVisible()
