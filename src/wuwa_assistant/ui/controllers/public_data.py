"""Non-blocking UI coordinator for explicit official public-data updates."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import cast

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from wuwa_assistant.public_data.client import PublicDataError
from wuwa_assistant.public_data.models import PublicDataFeed, PublicDataSyncResult
from wuwa_assistant.public_data.schedule import countdown, schedule_items
from wuwa_assistant.public_data.service import OfficialPublicDataService
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.pages.gacha import GachaPage
from wuwa_assistant.ui.pages.settings import SettingsPage
from wuwa_assistant.ui.pages.tasks_events import TasksEventsPage

logger = logging.getLogger(__name__)


def farming_activity_notices(
    feed: PublicDataFeed | None, *, now: datetime | None = None
) -> tuple[str, ...]:
    moment = now or datetime.now(UTC)
    if feed is None:
        return ("尚無公開活動快取; 更新後顯示技能素材/無音區加倍。",)
    lines: list[str] = []
    for item in schedule_items(feed.notices, kind="event"):
        if item.bonus_kind is None or (item.ends_at is not None and item.ends_at <= moment):
            continue
        if item.starts_at is not None and item.starts_at > moment:
            timing = "距開始 " + countdown(item.starts_at, now=moment)
        elif item.starts_at is None:
            timing = item.start_label + "; " + item.end_label
        else:
            timing = "剩餘 " + countdown(item.ends_at, now=moment)
        limit = (
            f"每日 {item.daily_bonus_limit} 次; "
            if item.daily_bonus_limit is not None
            else "每日加倍次數請見遊戲; "
        )
        lines.append(f"{item.title} · {timing}。{limit}即使目前無缺口也可優先使用加倍。")
    if not lines:
        lines.append("目前快取未列出仍有效的加倍活動; 請以遊戲內活動為準。")
    lines.append(f"公開資料更新: {feed.fetched_at}; 不自動推測已領取的加倍次數。")
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class _WorkerResult:
    request_id: int
    result: PublicDataSyncResult | None = None
    error: str | None = None


class OfficialPublicDataPresenter(QObject):
    """Load cache first, then update only when opted in or explicitly requested."""

    _worker_completed = Signal(object)
    feed_changed = Signal(object)

    def __init__(
        self,
        *,
        service: OfficialPublicDataService,
        settings_page: SettingsPage,
        tasks_page: TasksEventsPage,
        gacha_page: GachaPage,
        auto_update: Callable[[], bool] = lambda: False,
        supplementary_sync: Callable[[], str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._settings_page = settings_page
        self._tasks_page = tasks_page
        self._gacha_page = gacha_page
        self._auto_update = auto_update
        self._supplementary_sync = supplementary_sync
        self._initial_cache = True
        self._feed: PublicDataFeed | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(6 * 60 * 60 * 1000)
        self._timer.timeout.connect(self._automatic_update)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(60_000)
        self._clock_timer.timeout.connect(lambda: self.feed_changed.emit(self._feed))
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-public-data")
        self._lock = RLock()
        self._futures: set[Future[object]] = set()
        self._request_id = 0
        self._closing = False
        self._worker_completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        settings_page.public_data_update_requested.connect(self.update_from_official)
        settings_page.settings_saved.connect(lambda _values: self._automatic_update())

    def activity_notices(self, _day: object = None) -> tuple[str, ...]:
        return farming_activity_notices(self._feed)

    def initialize(self) -> None:
        """Read the local cache in a worker; no network request is made."""

        self._submit(self._service.load_cached, connecting=False)
        self._timer.start()
        self._clock_timer.start()

    @Slot()
    def _automatic_update(self) -> None:
        if self._auto_update() and not self._futures:
            self.update_from_official()

    @Slot()
    def update_from_official(self) -> None:
        if self._closing:
            return
        self._settings_page.set_public_data_sync_state(
            "正在更新官方時程與公開無音區資料; 不會傳送帳號或本機資料。",
            busy=True,
        )
        logger.info("public_data_ui action=explicit_update status=started")
        self._submit(self._synchronize, connecting=True)

    def _synchronize(self) -> PublicDataSyncResult:
        from dataclasses import replace

        result = self._service.synchronize()
        if self._supplementary_sync is not None:
            result = replace(result, message=result.message + " " + self._supplementary_sync())
        return result

    def prepare_shutdown(self) -> None:
        """Quiesce UI callbacks and cancel queued reads without waiting."""

        self._closing = True
        self._timer.stop()
        self._clock_timer.stop()
        self._executor.prepare_shutdown()

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        with self._lock:
            self._futures.clear()
        return True

    def _submit(
        self,
        work: Callable[[], PublicDataSyncResult],
        *,
        connecting: bool,
    ) -> None:
        if self._closing:
            return
        with self._lock:
            self._request_id += 1
            request_id = self._request_id

        def run() -> _WorkerResult:
            try:
                return _WorkerResult(request_id, result=work())
            except (PublicDataError, OSError, ValueError) as exc:
                return _WorkerResult(request_id, error=str(exc))

        future = cast(Future[object], self._executor.submit(run, read_only=not connecting))
        with self._lock:
            self._futures.add(future)

        def completed(done: Future[object]) -> None:
            with self._lock:
                self._futures.discard(done)
            if not self._closing:
                self._worker_completed.emit(done.result())

        future.add_done_callback(completed)

    @Slot(object)
    def _handle_result(self, payload: object) -> None:
        if self._closing or not isinstance(payload, _WorkerResult):
            return
        if payload.request_id != self._request_id:
            return
        if payload.error is not None or payload.result is None:
            logger.warning("public_data_ui status=failed error_code=PublicDataError")
            self._settings_page.set_public_data_sync_state(
                f"官方公開資料更新失敗: {payload.error or '未知錯誤'}",
                error=True,
            )
            return
        result = payload.result
        self._feed = result.feed
        notices = () if result.feed is None else result.feed.notices
        logger.info(
            "public_data_ui status=rendered count=%d cache=%s not_modified=%s",
            len(notices),
            result.used_cache,
            result.not_modified,
        )
        self._tasks_page.set_official_notices(notices)
        self._gacha_page.set_official_notices(notices)
        timestamp = ""
        if result.feed is not None:
            timestamp = f" 上次成功更新: {result.feed.fetched_at}。"
        self._settings_page.set_public_data_sync_state(result.message + timestamp)
        self.feed_changed.emit(result.feed)
        if self._initial_cache:
            self._initial_cache = False
            QTimer.singleShot(0, self._automatic_update)


__all__ = ["OfficialPublicDataPresenter"]
