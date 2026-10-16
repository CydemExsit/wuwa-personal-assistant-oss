"""Short-lived weekend notification runner, independent of capture and the main window."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files

from wuwa_assistant.public_data.client import PublicDataError
from wuwa_assistant.public_data.models import PublicDataFeed
from wuwa_assistant.public_data.recurring import challenge_periods
from wuwa_assistant.public_data.schedule import (
    ASIA_ZONE,
    countdown,
    schedule_items,
    weekend_reminders,
)
from wuwa_assistant.public_data.schedule_state import ScheduleCompletionService
from wuwa_assistant.public_data.service import OfficialPublicDataService
from wuwa_assistant.reminders.windows_task import eligible_weekend
from wuwa_assistant.storage import AppPaths, create_database

ENABLED_KEY = "reminders/background_enabled"
HOUR_KEY = "reminders/weekend_hour"
MINUTE_KEY = "reminders/weekend_minute"
AUTO_UPDATE_KEY = "public_data/auto_update"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReminderItem:
    key: str
    title: str
    detail: str


def pending_reminders(
    feed: PublicDataFeed | None,
    *,
    now: datetime,
    completed_keys: Iterable[str] = (),
) -> tuple[ReminderItem, ...]:
    completed = frozenset(completed_keys)
    if not eligible_weekend(now, 0):
        return ()
    items = [
        ReminderItem(item.key, item.title, "剩餘 " + countdown(item.ends_at, now=now))
        for item in weekend_reminders(
            (*schedule_items(feed.notices if feed else ()), *challenge_periods(now)),
            now=now,
            completed_keys=completed,
        )
    ]
    return tuple(items)


def unsent_reminders(
    items: tuple[ReminderItem, ...], ledger: object, *, now: datetime
) -> tuple[ReminderItem, ...]:
    today = now.astimezone(ASIA_ZONE).date().isoformat()
    sent = ledger if isinstance(ledger, dict) and ledger.get("date") == today else {}
    keys = sent.get("keys", [])
    known = frozenset(key for key in keys if isinstance(key, str)) if isinstance(keys, list) else ()
    return tuple(item for item in items if item.key not in known)


def show_desktop_notification(title: str, message: str) -> bool:
    """Submit a native Qt/Windows notification; OS focus/notification settings still apply."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    application = QApplication.instance()
    if application is None:
        application = QApplication(["wuwa-weekend-reminder"])
    if not QSystemTrayIcon.isSystemTrayAvailable() or not QSystemTrayIcon.supportsMessages():
        return False
    icon = QIcon(str(files("wuwa_assistant.ui.styles").joinpath("app.ico")))
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("鳴潮個人養成助理・週末提醒")
    tray.show()
    application.processEvents()
    tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 12_000)
    loop = QEventLoop()
    QTimer.singleShot(15_000, loop.quit)
    loop.exec()
    tray.hide()
    return True


def run_background_reminders(
    paths: AppPaths,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    notify: Callable[[str, str], bool] = show_desktop_notification,
) -> int:
    """Check primary data only; do not run onboarding, staged restore, OCR or capture."""
    from PySide6.QtCore import QLockFile
    from sqlalchemy import select

    from wuwa_assistant.storage.orm import DataSetRecord

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("reminder time must be timezone-aware")
    if not paths.database_path.is_file():
        return 0
    lock = QLockFile(str(paths.config_dir / "weekend-reminders.lock"))
    lock.setStaleLockTime(300_000)
    if not lock.tryLock(0):
        return 0
    report: dict[str, object] = {"checked_at": moment.isoformat(), "dry_run": dry_run}
    try:
        with create_database(paths) as database:
            settings = database.settings
            if settings.get(ENABLED_KEY, False) is not True:
                report["status"] = "disabled"
                return 0
            hour = settings.get(HOUR_KEY, 10)
            if type(hour) is not int or not 0 <= hour <= 23:
                hour = 10
            minute = settings.get(MINUTE_KEY, 0)
            if type(minute) is not int or not 0 <= minute <= 59:
                minute = 0
            if not eligible_weekend(moment, hour, minute):
                report["status"] = "outside_weekend_window"
                return 0
            with database.session_factory() as session:
                dataset = session.scalar(
                    select(DataSetRecord)
                    .where(DataSetRecord.kind == "primary", DataSetRecord.is_active.is_(True))
                    .order_by(DataSetRecord.created_at)
                )
                data_set_id = None if dataset is None else dataset.id
            if data_set_id is None:
                report["status"] = "no_active_primary_dataset"
                return 0
            public = OfficialPublicDataService(paths.public_data_cache_dir)
            result = public.load_cached()
            if not dry_run and settings.get(AUTO_UPDATE_KEY, False) is True:
                try:
                    result = public.synchronize()
                except PublicDataError:
                    # A first-time offline start still has the locally known endgame cycles.
                    logger.warning("weekend_public_refresh_unavailable_using_local_state")
            completed = ScheduleCompletionService(settings).completed_keys(data_set_id=data_set_id)
            pending = pending_reminders(result.feed, now=moment, completed_keys=completed)
            digest = hashlib.sha256(data_set_id.encode()).hexdigest()
            ledger_key = f"reminders/submitted/{digest}"
            ledger = settings.get(ledger_key)
            due = unsent_reminders(pending, ledger, now=moment)
            report.update(
                status="dry_run" if dry_run else "nothing_due",
                due_keys=[item.key for item in due],
                cache_updated_at=None if result.feed is None else result.feed.fetched_at,
            )
            if not due or dry_run:
                return 0
            # One digest avoids overlapping Windows balloons and a notification flood.
            body = "\n".join(f"{item.title}:{item.detail}" for item in due[:3])
            if len(due) > 3:
                body += f"\n另有 {len(due) - 3} 項,請開啟活動與待辦查看。"
            body += "\n完成後可在活動頁勾選;未勾選不代表遊戲內未完成。"
            if not notify("鳴潮・週末提醒", body):
                report["status"] = "notification_unavailable"
                return 3
            today = moment.astimezone(ASIA_ZONE).date().isoformat()
            previous = (
                ledger.get("keys", [])
                if isinstance(ledger, dict) and ledger.get("date") == today
                else []
            )
            keys = (
                [key for key in previous if isinstance(key, str)]
                if isinstance(previous, list)
                else []
            )
            settings.set(
                ledger_key, {"date": today, "keys": sorted(set(keys + [x.key for x in due]))}
            )
            report["status"] = "submitted_to_windows"
            return 0
    except (OSError, ValueError, RuntimeError) as exc:
        logger.exception("weekend_reminder_failed type=%s", type(exc).__name__)
        report.update(status="failed", error_type=type(exc).__name__)
        return 2
    finally:
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        report_path = paths.logs_dir / "reminder-last-run.json"
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report_path)
        lock.unlock()
