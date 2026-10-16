"""Compact, link-safe rendering of official public notices."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from html import escape
from itertools import groupby

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.public_data.models import OfficialNotice, OfficialSchedule
from wuwa_assistant.public_data.recurring import challenge_periods
from wuwa_assistant.public_data.schedule import (
    ASIA_ZONE,
    countdown,
    schedule_items,
    weekend_reminders,
)


class OfficialFeedWidget(QGroupBox):
    def __init__(self, title: str, *, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setObjectName("OfficialFeed")
        self.browser = QTextBrowser()
        self.browser.setObjectName("OfficialFeedBrowser")
        self.browser.setOpenExternalLinks(True)
        self.browser.document().setDefaultStyleSheet("a { color: #90d5ec; }")
        self.browser.setMinimumHeight(120)
        self.browser.setMaximumHeight(190)
        layout = QVBoxLayout(self)
        layout.addWidget(self.browser)
        self.set_notices(())

    def set_notices(self, notices: Sequence[OfficialNotice]) -> None:
        if not notices:
            self.browser.setHtml(
                "<p><b>尚無官方公開資料。</b><br>"
                "連線時會更新庫洛官方公開時程; 離線時保留上次資料。</p>"
            )
            return
        rows: list[str] = []
        for notice in notices[:12]:
            if notice.starts_on is not None and notice.ends_on is not None:
                date_text = f"{notice.starts_on} ~ {notice.ends_on}"
            else:
                date_text = "日期需人工確認"
            rows.append(
                "<p style='margin: 5px 0'>"
                f"<a href='{escape(notice.official_url, quote=True)}'>"
                f"{escape(notice.title)}</a><br>"
                f"<small>{escape(date_text)} · 庫洛官方網站</small></p>"
            )
        self.browser.setHtml("".join(rows))


class BannerCountdownWidget(QGroupBox):
    """All simultaneous character banners, with explicit unknown opening times."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Asia 卡池時程", parent)
        self._notices: tuple[OfficialNotice, ...] = ()
        self.browser = QTextBrowser()
        self.browser.setObjectName("BannerCountdownBrowser")
        self.browser.setOpenExternalLinks(True)
        self.browser.document().setDefaultStyleSheet("a { color: #90d5ec; }")
        self.browser.setMinimumHeight(360)
        self.setMinimumHeight(420)
        QVBoxLayout(self).addWidget(self.browser)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def set_notices(self, notices: tuple[OfficialNotice, ...]) -> None:
        self._notices = notices
        self.refresh()

    def refresh(self, now: datetime | None = None) -> None:
        instant = now or datetime.now(UTC)
        items = schedule_items(self._notices, kind="gacha")
        current = [
            item
            for item in items
            if item.ends_at is not None
            and item.ends_at > instant
            and item.starts_at is not None
            and item.starts_at <= instant
        ]
        ending = [
            item
            for item in items
            if item.starts_at is None and item.ends_at is not None and item.ends_at > instant
        ]
        future = [item for item in items if item.starts_at is not None and item.starts_at > instant]
        next_start = min((item.starts_at for item in future if item.starts_at), default=None)
        upcoming = [item for item in future if item.starts_at == next_start]
        unknown = [item for item in items if item.ends_at is None and item.starts_at is None]
        rows = [
            "<table width='100%' cellspacing='16'><tr><td width='49%' valign='top'>"
            "<h2>當期卡池</h2>"
        ]
        rows.extend(self._period_cards((*current, *ending), instant, upcoming=False))
        if not current and not ending:
            rows.append("<p>目前沒有可確認仍在期間內的卡池資料。</p>")
        rows.append("</td><td width='49%' valign='top'><h2>下一期</h2>")
        rows.extend(self._period_cards(upcoming, instant, upcoming=True))
        if not upcoming:
            rows.append("<p>目前資料未取得下一期完整時程; 可更新資料或查看官方公告。</p>")
        rows.append("</td></tr></table>")
        if unknown:
            rows.append("<h3>時程待確認</h3>")
            rows.extend(self._card(item, instant, upcoming=False) for item in unknown)
        scroll = self.browser.verticalScrollBar().value()
        self.browser.setHtml("".join(rows))
        self.browser.verticalScrollBar().setValue(scroll)

    @staticmethod
    def _period_cards(
        items: Sequence[OfficialSchedule], now: datetime, *, upcoming: bool
    ) -> list[str]:
        def endpoint(item: OfficialSchedule) -> str:
            value = item.starts_at if upcoming else item.ends_at
            return value.isoformat() if value else ""

        cards = []
        for _key, group in groupby(sorted(items, key=endpoint), key=endpoint):
            members = tuple(group)
            first = members[0]
            deadline = first.starts_at if upcoming else first.ends_at
            names = tuple(
                dict.fromkeys(name for item in members for name in item.five_star_characters)
            )
            four = tuple(
                dict.fromkeys(name for item in members for name in item.four_star_characters)
            )
            links = "<br>".join(
                f"<a href='{escape(item.official_url, quote=True)}'>{escape(item.title)}</a>"
                for item in members
            )
            partial = (
                "<p>版本更新後開啟 (確切開始時間待確認)</p>"
                if any(item.starts_at is None for item in members)
                else ""
            )
            cards.append(
                "<p style='font-size:20px'><b>"
                f"{escape('、'.join(names) or '五星角色待確認')}</b></p>"
                f"<p>4 星: {escape('、'.join(four) or '四星角色待確認')}</p>"
                f"<p>距離{'開啟' if upcoming else '結束'}<br>"
                f"<b style='font-size:20px'>{countdown(deadline, now=now)}</b><br>"
                f"{escape(_moment_text(deadline) if deadline else '時間待確認')}</p>"
                f"{partial}<p>{links}</p>"
            )
        return cards

    @staticmethod
    def _card(item: OfficialSchedule, now: datetime, *, upcoming: bool) -> str:
        characters = "、".join(item.five_star_characters) or "五星角色待確認"
        four = "、".join(item.four_star_characters) or "四星角色待確認"
        endpoint = item.starts_at if upcoming else item.ends_at
        action = "開啟" if upcoming else "結束"
        unknown_start = (
            f"<br>{escape(item.start_label)} (確切開始時間待確認)" if item.starts_at is None else ""
        )
        return (
            f"<p><b style='font-size:18px'>{escape(characters)}</b><br>"
            f"4 星: {escape(four)}<br>"
            f"距離{action}: <b>{countdown(endpoint, now=now)}</b>"
            f"<br>{escape(_moment_text(endpoint) if endpoint else item.end_label)}"
            f"{unknown_start}<br><a href='{escape(item.official_url, quote=True)}'>"
            f"{escape(item.title)} · 官方公告</a></p>"
        )


class EventCountdownWidget(QWidget):
    completion_changed = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notices: tuple[OfficialNotice, ...] = ()
        self._completed: frozenset[str] = frozenset()
        self._rows: list[tuple[OfficialSchedule, QCheckBox, QLabel]] = []
        self._signature: tuple[object, ...] = ()
        self._last_now: datetime | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.challenge_grid = QGridLayout()
        layout.addWidget(QLabel("週期挑戰 · 海墟 / 深塔"))
        layout.addLayout(self.challenge_grid)
        layout.addWidget(QLabel("進行中活動 · 依結束時間排序"))
        self.events_layout = QGridLayout()
        layout.addLayout(self.events_layout)
        self.empty_label = QLabel("目前沒有進行中的限時活動。")
        layout.addWidget(self.empty_label)
        self.upcoming_button = QPushButton("尚未開始")
        self.upcoming_button.setCheckable(True)
        self.upcoming_box = QWidget()
        self.upcoming_layout = QGridLayout(self.upcoming_box)
        self.upcoming_layout.setContentsMargins(0, 0, 0, 0)
        self.upcoming_box.hide()
        self.upcoming_button.toggled.connect(self.upcoming_box.setVisible)
        layout.addWidget(self.upcoming_button)
        layout.addWidget(self.upcoming_box)
        layout.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def set_notices(self, notices: tuple[OfficialNotice, ...]) -> None:
        self._notices = notices
        self._signature = ()
        self.refresh()

    def set_completed(self, keys: frozenset[str]) -> None:
        self._completed = keys
        self.refresh()

    def _changed(self, key: str, checked: bool) -> None:
        self._completed = self._completed | {key} if checked else self._completed - {key}
        self.completion_changed.emit(key, checked)
        self.refresh(self._last_now)

    def _card(self, item: OfficialSchedule) -> QWidget:
        box = QFrame()
        box.setObjectName("EventCompactCard")
        box.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 10, 12, 10)
        # The OSS edition links to announcements without redistributing artwork.
        info = QVBoxLayout()
        title = QLabel(item.title)
        title.setWordWrap(True)
        title.setObjectName("EventCardTitle")
        check = QCheckBox("本期已完成")
        check.toggled.connect(lambda checked, key=item.key: self._changed(key, checked))
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setOpenExternalLinks(True)
        info.addWidget(title)
        info.addWidget(label)
        info.addWidget(check)
        row.addLayout(info, 1)
        self._rows.append((item, check, label))
        return box

    def _rebuild(self, now: datetime, columns: int) -> None:
        for grid in (self.challenge_grid, self.events_layout, self.upcoming_layout):
            while grid.count():
                entry = grid.takeAt(0)
                if entry and (widget := entry.widget()) is not None:
                    widget.hide()
                    widget.deleteLater()
        self._rows.clear()
        for index, item in enumerate(challenge_periods(now)):
            self.challenge_grid.addWidget(self._card(item), index // columns, index % columns)
        active: list[OfficialSchedule] = []
        future: list[OfficialSchedule] = []
        for item in sorted(
            schedule_items(self._notices, kind="event"),
            key=lambda item: item.ends_at or datetime.max.replace(tzinfo=UTC),
        ):
            if item.ends_at is not None and item.ends_at <= now:
                continue
            (future if item.starts_at is not None and item.starts_at > now else active).append(item)
        for items, grid in ((active, self.events_layout), (future, self.upcoming_layout)):
            for index, item in enumerate(items):
                grid.addWidget(self._card(item), index // columns, index % columns)
            for column in range(2):
                grid.setColumnStretch(column, 1 if column < columns else 0)
        self.empty_label.setVisible(not active)
        self.upcoming_button.setText(
            f"{'收合' if self.upcoming_button.isChecked() else '展開'}尚未開始 ({len(future)})"
        )
        self.upcoming_button.setVisible(bool(future))
        self.upcoming_box.setVisible(bool(future) and self.upcoming_button.isChecked())

    def refresh(self, now: datetime | None = None) -> None:
        instant = now or datetime.now(UTC)
        self._last_now = instant
        columns = 2 if self.width() >= 660 else 1
        events = schedule_items(self._notices, kind="event")
        signature = (
            columns,
            self.upcoming_button.isChecked(),
            *(item.key for item in challenge_periods(instant)),
            *(
                (
                    item.key,
                    item.starts_at is not None and item.starts_at > instant,
                    item.ends_at is not None and item.ends_at <= instant,
                )
                for item in events
            ),
        )
        if signature != self._signature:
            self._signature = signature
            self._rebuild(instant, columns)
        urgent = {
            item.key
            for item in weekend_reminders(
                (row[0] for row in self._rows), now=instant, completed_keys=self._completed
            )
        }
        for item, check, label in self._rows:
            check.blockSignals(True)
            check.setChecked(item.key in self._completed)
            check.blockSignals(False)
            upcoming = item.starts_at is not None and item.starts_at > instant
            deadline = item.starts_at if upcoming else item.ends_at
            status = "開始" if upcoming else "結束"
            hint = " · 週末提醒" if item.key in urgent else ""
            provenance = f"<br>{escape(item.start_label)}" if item.key.startswith("cycle:") else ""
            link = (
                ""
                if item.key.startswith("cycle:")
                else f" · <a style='color:#90d5ec' href='{escape(item.official_url, quote=True)}'>"
                "公告</a>"
            )
            label.setText(
                f"距離{status} <b>{countdown(deadline, now=instant)}</b>{hint}<br>"
                f"{escape(_moment_text(deadline) if deadline else item.end_label)}"
                f"{link}{provenance}"
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "upcoming_button"):
            self.refresh()


def _moment_text(value: datetime) -> str:
    return value.astimezone(ASIA_ZONE).strftime("%Y-%m-%d %H:%M (Asia / UTC+8)")
