"""Two-phase shutdown: Qt preparation on the GUI thread, blocking drains off it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Signal

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ShutdownAction:
    finish: Callable[[], object]
    prepare: Callable[[], object] | None


class ShutdownCoordinator(QObject):
    """Keep Qt objects alive until every registered worker has actually stopped."""

    finished = Signal(bool)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._actions: list[_ShutdownAction] = []
        self._pending: list[_ShutdownAction] | None = None
        self._worker: Thread | None = None
        self._failed: list[_ShutdownAction] = []
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    @property
    def has_actions(self) -> bool:
        return bool(self._actions)

    @property
    def running(self) -> bool:
        return self._worker is not None

    def register(
        self, finish: Callable[[], object], *, prepare: Callable[[], object] | None = None
    ) -> None:
        action = _ShutdownAction(finish, prepare)
        if action not in self._actions:
            self._actions.append(action)

    def start(self) -> None:
        if self.running:
            return
        actions = list(reversed(self._actions)) if self._pending is None else self._pending
        self._failed = []
        prepared: list[_ShutdownAction] = []
        # All producers quiesce before any joins start. Timer.stop and every
        # other Qt operation remain on this caller's GUI thread.
        for action in actions:
            try:
                if action.prepare is None:
                    # Older registrations carry no thread-safety guarantee.
                    if action.finish() is False:
                        self._failed.append(action)
                else:
                    action.prepare()
                    prepared.append(action)
            except Exception:
                _LOGGER.exception("shutdown_preparation_failed")
                self._failed.append(action)

        def drain() -> None:
            for action in prepared:
                try:
                    if action.finish() is False:
                        self._failed.append(action)
                except Exception:
                    _LOGGER.exception("shutdown_worker_failed")
                    self._failed.append(action)

        self._worker = Thread(target=drain, name="wuwa-shutdown", daemon=False)
        self._worker.start()
        self._timer.start()

    def _poll(self) -> None:
        if self._worker is None or self._worker.is_alive():
            return
        self._worker.join()
        self._worker = None
        self._timer.stop()
        self._pending = list(self._failed)
        self.finished.emit(not self._pending)

    def finish_blocking(self) -> bool:
        """Fallback for an already-ended event loop before resource disposal."""

        if self._pending == [] and not self.running:
            return True
        if not self.running:
            self.start()
        assert self._worker is not None
        self._worker.join()
        self._worker = None
        self._timer.stop()
        self._pending = list(self._failed)
        return not self._pending
