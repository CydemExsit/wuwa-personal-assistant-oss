from __future__ import annotations

from threading import Event, get_ident

import pytest
from PySide6.QtCore import QTimer
from pytestqt.qtbot import QtBot

from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.main_window import MainWindow

from .helpers import MemorySettings


def test_shutdown_cancels_queued_reads_but_drains_accepted_writes(qtbot: QtBot) -> None:
    window = MainWindow(MemorySettings())
    qtbot.addWidget(window)
    executor = BackgroundExecutor(thread_name_prefix="shutdown-test")
    started, release = Event(), Event()
    written: list[str] = []
    gui_thread = get_ident()
    phases: list[tuple[str, int]] = []

    def slow_read() -> None:
        started.set()
        assert release.wait(5)

    executor.submit(slow_read, read_only=True)
    assert started.wait(2)
    obsolete = executor.submit(lambda: written.append("obsolete-read"), read_only=True)
    required = executor.submit(lambda: written.append("saved"))

    def prepare() -> None:
        phases.append(("prepare", get_ident()))
        executor.prepare_shutdown()

    def finish() -> None:
        phases.append(("finish", get_ident()))
        executor.shutdown()

    window.register_shutdown(finish, prepare=prepare)
    window.show()
    pulses: list[int] = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: pulses.append(1))
    timer.start(10)
    try:
        assert not window.close()
        assert window.isVisible()
        assert "正在安全關閉" in window.statusBar().currentMessage()
        assert obsolete.cancelled()
        assert not required.done()
        qtbot.waitUntil(lambda: len(pulses) >= 3)
        # Repeated close must not start another drain or discard required work.
        window.close()
        assert [phase for phase, _ in phases].count("prepare") == 1
        release.set()
        qtbot.waitUntil(lambda: not window.isVisible())
        assert required.done() and written == ["saved"]
        assert phases == [("prepare", gui_thread), ("finish", phases[1][1])]
        assert phases[1][1] != gui_thread
        assert window.finish_shutdown()
    finally:
        release.set()
        window.finish_shutdown()
        timer.stop()


@pytest.mark.parametrize("failure", ["exception", "false", "prepare"])
def test_shutdown_failure_keeps_window_open_and_retries(qtbot: QtBot, failure: str) -> None:
    window = MainWindow(MemorySettings())
    qtbot.addWidget(window)
    attempts = 0
    prepares = 0

    def prepare() -> None:
        nonlocal prepares
        prepares += 1
        if failure == "prepare" and prepares == 1:
            raise RuntimeError("preparation failed")

    def finish() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure == "exception":
            raise RuntimeError("worker failed")
        return not (attempts == 1 and failure == "false")

    window.register_shutdown(finish, prepare=prepare)
    window.show()
    window.close()
    qtbot.waitUntil(lambda: window._shutdown_retry.isVisible())
    assert window.isVisible()
    assert "尚未完成" in window.statusBar().currentMessage()
    window._shutdown_retry.click()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert prepares == 2
    assert attempts == (1 if failure == "prepare" else 2)


def test_event_loop_exit_fallback_drains_and_prepares_on_gui_thread(qtbot: QtBot) -> None:
    window = MainWindow(MemorySettings())
    qtbot.addWidget(window)
    caller = get_ident()
    calls: list[tuple[str, int]] = []
    window.register_shutdown(
        lambda: calls.append(("finish", get_ident())),
        prepare=lambda: calls.append(("prepare", get_ident())),
    )
    assert window.finish_shutdown()
    assert window.finish_shutdown()
    assert len(calls) == 2
    assert calls[0] == ("prepare", caller)
    assert calls[1][0] == "finish" and calls[1][1] != caller
