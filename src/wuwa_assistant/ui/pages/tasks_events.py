"""Traditional-Chinese task and event management page."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractTableModel,
    QDateTime,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.public_data.models import OfficialNotice
from wuwa_assistant.ui.widgets import OfficialFeedWidget
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox
from wuwa_assistant.ui.widgets.official_feed import EventCountdownWidget

TAIPEI = ZoneInfo("Asia/Taipei")
_INVALID_INDEX = QModelIndex()
type ModelIndex = QModelIndex | QPersistentModelIndex
_INACTIVE_EVENT_STATUSES = frozenset({"completed", "ignored", "cancelled", "ended"})


def _required(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label}不可空白")


def _aware(value: datetime | None, label: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{label}必須包含時區")


@dataclass(frozen=True, slots=True)
class TaskView:
    identifier: str
    title: str
    task_type: str
    due_at: datetime | None
    priority: int
    recurrence: str | None = None
    event_id: str | None = None
    event_title: str | None = None
    completed_at: datetime | None = None
    snoozed_until: datetime | None = None
    hidden: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        _required(self.identifier, "待辦識別碼")
        _required(self.title, "待辦標題")
        _required(self.task_type, "待辦類型")
        if self.priority < 0:
            raise ValueError("priority must be nonnegative")
        for value, label in (
            (self.due_at, "due_at"),
            (self.completed_at, "completed_at"),
            (self.snoozed_until, "snoozed_until"),
        ):
            _aware(value, label)


@dataclass(frozen=True, slots=True)
class EventView:
    identifier: str
    title: str
    event_type: str
    starts_at: datetime | None
    ends_at: datetime | None
    priority: int
    status: str
    hidden: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        _required(self.identifier, "活動識別碼")
        _required(self.title, "活動標題")
        _required(self.event_type, "活動類型")
        _required(self.status, "活動狀態")
        if self.priority < 0:
            raise ValueError("priority must be nonnegative")
        _aware(self.starts_at, "starts_at")
        _aware(self.ends_at, "ends_at")
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at < self.starts_at
        ):
            raise ValueError("活動結束時間不可早於開始時間")


@dataclass(frozen=True, slots=True)
class TaskSaveRequest:
    identifier: str | None
    title: str
    task_type: str
    due_at: datetime | None
    priority: int
    recurrence_frequency: str | None
    recurrence_interval: int
    event_id: str | None
    hidden: bool
    notes: str
    preserve_unknown_recurrence: bool = False

    def __post_init__(self) -> None:
        _required(self.title, "待辦標題")
        _required(self.task_type, "待辦類型")
        _aware(self.due_at, "due_at")
        if self.priority < 0:
            raise ValueError("priority must be nonnegative")
        if self.recurrence_frequency not in {None, "daily", "weekly"}:
            raise ValueError("recurrence_frequency is invalid")
        if not 1 <= self.recurrence_interval <= 365:
            raise ValueError("recurrence_interval must be within 1..365")
        if type(self.preserve_unknown_recurrence) is not bool:
            raise ValueError("preserve_unknown_recurrence must be boolean")
        if self.preserve_unknown_recurrence and self.recurrence_frequency is not None:
            raise ValueError("unknown recurrence cannot also select a known frequency")


@dataclass(frozen=True, slots=True)
class EventSaveRequest:
    identifier: str | None
    title: str
    event_type: str
    starts_at: datetime | None
    ends_at: datetime | None
    priority: int
    status: str
    hidden: bool
    notes: str

    def __post_init__(self) -> None:
        EventView(
            self.identifier or "new-event",
            self.title,
            self.event_type,
            self.starts_at,
            self.ends_at,
            self.priority,
            self.status,
            self.hidden,
            self.notes,
        )


@dataclass(frozen=True, slots=True)
class SnoozeTaskRequest:
    task_id: str
    until: datetime

    def __post_init__(self) -> None:
        _required(self.task_id, "待辦識別碼")
        _aware(self.until, "until")


@dataclass(frozen=True, slots=True)
class DueSummaryView:
    overdue: int = 0
    today: int = 0
    next_three_days: int = 0
    this_week: int = 0

    def __post_init__(self) -> None:
        if min(self.overdue, self.today, self.next_three_days, self.this_week) < 0:
            raise ValueError("due summary counts must be nonnegative")


@dataclass(frozen=True, slots=True)
class TasksEventsSnapshot:
    tasks: tuple[TaskView, ...] = ()
    events: tuple[EventView, ...] = ()
    due: DueSummaryView = DueSummaryView()
    as_of: datetime | None = None


class _TaskTableModel(QAbstractTableModel):
    _HEADERS = ("待辦", "類型", "到期", "重複", "優先級", "狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[TaskView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.identifier
        if role == int(Qt.ItemDataRole.DisplayRole):
            return (
                row.title,
                row.task_type,
                _datetime_text(row.due_at),
                _recurrence_text(row.recurrence),
                row.priority,
                _task_status(row),
            )[index.column()]
        if role == int(Qt.ItemDataRole.EditRole):
            return (
                row.title,
                row.task_type,
                row.due_at or datetime.max.replace(tzinfo=UTC),
                row.recurrence or "",
                row.priority,
                row.completed_at is not None,
            )[index.column()]
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == int(Qt.ItemDataRole.DisplayRole)
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def set_rows(self, rows: Sequence[TaskView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, index: ModelIndex) -> TaskView | None:
        return (
            self._rows[index.row()]
            if index.isValid() and 0 <= index.row() < len(self._rows)
            else None
        )


class _EventTableModel(QAbstractTableModel):
    _HEADERS = ("活動", "類型", "開始", "結束", "優先級", "狀態")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[EventView] = []

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: ModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == int(Qt.ItemDataRole.UserRole):
            return row.identifier
        if role == int(Qt.ItemDataRole.DisplayRole):
            return (
                row.title,
                row.event_type,
                _datetime_text(row.starts_at),
                _datetime_text(row.ends_at),
                row.priority,
                "已隱藏" if row.hidden else row.status,
            )[index.column()]
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == int(Qt.ItemDataRole.DisplayRole)
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def set_rows(self, rows: Sequence[EventView]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, index: ModelIndex) -> EventView | None:
        return (
            self._rows[index.row()]
            if index.isValid() and 0 <= index.row() < len(self._rows)
            else None
        )


class _MetricCard(QFrame):
    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        label = QLabel(title)
        label.setObjectName("MetricTitle")
        self.value_label = QLabel("0")
        self.value_label.setObjectName(object_name)
        self.value_label.setProperty("metric", True)
        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(self.value_label)


class TasksEventsPage(QWidget):
    """Local task/event workspace; all persistence is delegated through signals."""

    refresh_requested = Signal()
    task_save_requested = Signal(object)
    task_complete_requested = Signal(str)
    task_snooze_requested = Signal(object)
    task_delete_requested = Signal(str)
    event_save_requested = Signal(object)
    event_delete_requested = Signal(str)
    schedule_completion_requested = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TasksEventsPage")
        self._tasks: tuple[TaskView, ...] = ()
        self._events: tuple[EventView, ...] = ()
        self._loading = False

        title = QLabel("活動與待辦")
        title.setObjectName("PageTitle")
        description = QLabel("活動剩不到 10 天時於週末提醒; 海墟 / 深塔各期分別記錄完成狀態。")
        description.setObjectName("PageDescription")
        description.setWordWrap(True)

        cards = QGridLayout()
        self.overdue_card = _MetricCard("已逾期", "TasksOverdueCount")
        self.today_card = _MetricCard("今日到期", "TasksTodayCount")
        self.three_day_card = _MetricCard("三日內", "TasksThreeDayCount")
        self.week_card = _MetricCard("本週其餘", "TasksWeekCount")
        for column, card in enumerate(
            (self.overdue_card, self.today_card, self.three_day_card, self.week_card)
        ):
            cards.addWidget(card, 0, column)

        self.show_inactive_check = QCheckBox("顯示已完成與隱藏項目")
        self.show_inactive_check.toggled.connect(self._apply_filters)
        self.refresh_button = QPushButton("重新整理")
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.status_label = QLabel("正在載入本機待辦…")
        self.status_label.setObjectName("TasksEventsStatus")
        self.status_label.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(self.show_inactive_check)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.status_label, 1)

        self.task_model = _TaskTableModel(self)
        self.task_table = _table("TasksTable", self.task_model)
        task_buttons = QHBoxLayout()
        self.task_add_button = QPushButton("新增待辦")
        self.task_edit_button = QPushButton("編輯")
        self.task_complete_button = QPushButton("完成")
        self.task_snooze_button = QPushButton("延後 1 日")
        self.task_delete_button = QPushButton("刪除")
        for button in (
            self.task_add_button,
            self.task_edit_button,
            self.task_complete_button,
            self.task_snooze_button,
            self.task_delete_button,
        ):
            task_buttons.addWidget(button)
        task_buttons.addStretch(1)
        self.task_add_button.clicked.connect(self._add_task)
        self.task_edit_button.clicked.connect(self._edit_task)
        self.task_complete_button.clicked.connect(self._complete_task)
        self.task_snooze_button.clicked.connect(self._snooze_task)
        self.task_delete_button.clicked.connect(self._delete_task)
        task_tab = QWidget()
        task_layout = QVBoxLayout(task_tab)
        task_layout.addLayout(task_buttons)
        task_layout.addWidget(self.task_table)

        self.event_model = _EventTableModel(self)
        self.event_table = _table("EventsTable", self.event_model)
        event_buttons = QHBoxLayout()
        self.event_add_button = QPushButton("新增活動")
        self.event_edit_button = QPushButton("編輯")
        self.event_delete_button = QPushButton("刪除")
        for button in (self.event_add_button, self.event_edit_button, self.event_delete_button):
            event_buttons.addWidget(button)
        event_buttons.addStretch(1)
        self.event_add_button.clicked.connect(self._add_event)
        self.event_edit_button.clicked.connect(self._edit_event)
        self.event_delete_button.clicked.connect(self._delete_event)
        event_tab = QWidget()
        event_layout = QVBoxLayout(event_tab)
        event_layout.addLayout(event_buttons)
        event_layout.addWidget(self.event_table)

        self.tabs = QTabWidget()
        self.tabs.addTab(task_tab, "待辦")
        self.tabs.addTab(event_tab, "活動")
        self.official_feed = OfficialFeedWidget("庫洛官方公開公告")
        official_tab = QWidget()
        official_layout = QVBoxLayout(official_tab)
        official_note = QLabel(
            "公告日期只有在官方文章內含唯一、明確的起訖範圍時才會顯示; 否則標記為需要人工確認。"
        )
        official_note.setWordWrap(True)
        official_layout.addWidget(official_note)
        official_layout.addWidget(self.official_feed)
        official_layout.addStretch(1)
        self.tabs.addTab(official_tab, "官方公告")

        self.countdown_feed = EventCountdownWidget()
        self.countdown_feed.completion_changed.connect(self.schedule_completion_requested)
        self.countdown_scroll = QScrollArea()
        self.countdown_scroll.setWidgetResizable(True)
        self.countdown_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.countdown_scroll.setWidget(self.countdown_feed)
        self.legacy_button = QPushButton("自訂待辦與舊資料")
        self.legacy_button.setCheckable(True)
        self.legacy_button.toggled.connect(self.tabs.setVisible)
        self.legacy_button.toggled.connect(self.show_inactive_check.setVisible)
        self.tabs.hide()
        # Keep existing records and editor functionality available on demand.
        legacy_cards = QWidget(self)
        legacy_cards.setLayout(cards)
        legacy_cards.hide()
        self.show_inactive_check.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(controls)
        layout.addWidget(self.countdown_scroll, 1)
        layout.addWidget(self.legacy_button)
        layout.addWidget(self.tabs, 1)

    @Slot(object)
    def set_official_notices(self, notices: object) -> None:
        if not isinstance(notices, tuple) or not all(
            isinstance(item, OfficialNotice) for item in notices
        ):
            raise TypeError("notices must be a tuple of OfficialNotice")
        self.official_feed.set_notices(notices)
        self.countdown_feed.set_notices(notices)

    def set_schedule_completed(self, keys: frozenset[str]) -> None:
        self.countdown_feed.set_completed(keys)

    @Slot(object)
    def set_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, TasksEventsSnapshot):
            raise TypeError("snapshot must be TasksEventsSnapshot")
        self._tasks = snapshot.tasks
        self._events = snapshot.events
        self.overdue_card.value_label.setText(str(snapshot.due.overdue))
        self.today_card.value_label.setText(str(snapshot.due.today))
        self.three_day_card.value_label.setText(str(snapshot.due.next_three_days))
        self.week_card.value_label.setText(str(snapshot.due.this_week))
        self._apply_filters()
        self.set_loading(False)
        self.status_label.setText("Asia 活動倒數與本期完成狀態。")
        self.status_label.setProperty("error", False)

    @Slot(bool, str)
    def set_loading(self, loading: bool, message: str = "正在處理本機待辦資料…") -> None:
        self._loading = loading
        self.tabs.setEnabled(not loading)
        self.countdown_feed.setEnabled(not loading)
        self.refresh_button.setEnabled(not loading)
        if loading:
            self.status_label.setText(message)
            self.status_label.setProperty("error", False)

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setText(f"待辦操作失敗: {message}")
        self.status_label.setProperty("error", True)

    @Slot(str)
    def show_status(self, message: str) -> None:
        self.set_loading(False)
        self.status_label.setText(message)
        self.status_label.setProperty("error", False)

    def _apply_filters(self) -> None:
        include_inactive = self.show_inactive_check.isChecked()
        self.task_model.set_rows(
            self._tasks
            if include_inactive
            else tuple(
                item for item in self._tasks if item.completed_at is None and not item.hidden
            )
        )
        self.event_model.set_rows(
            self._events
            if include_inactive
            else tuple(
                item
                for item in self._events
                if not item.hidden and item.status.casefold() not in _INACTIVE_EVENT_STATUSES
            )
        )

    def _selected_task(self) -> TaskView | None:
        return self.task_model.row(self.task_table.currentIndex())

    def _selected_event(self) -> EventView | None:
        return self.event_model.row(self.event_table.currentIndex())

    def _add_task(self) -> None:
        dialog = _TaskEditor(self._events, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.task_save_requested.emit(dialog.request())

    def _edit_task(self) -> None:
        selected = self._selected_task()
        if selected is None:
            self.show_error("請先選擇待辦")
            return
        dialog = _TaskEditor(self._events, selected, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.task_save_requested.emit(dialog.request())

    def _complete_task(self) -> None:
        selected = self._selected_task()
        if selected is None:
            self.show_error("請先選擇待辦")
            return
        self.task_complete_requested.emit(selected.identifier)

    def _snooze_task(self) -> None:
        selected = self._selected_task()
        if selected is None:
            self.show_error("請先選擇待辦")
            return
        now = datetime.now(TAIPEI)
        base = max(
            value for value in (now, selected.snoozed_until, selected.due_at) if value is not None
        )
        self.task_snooze_requested.emit(
            SnoozeTaskRequest(selected.identifier, base + timedelta(days=1))
        )

    def _delete_task(self) -> None:
        selected = self._selected_task()
        if selected is not None and _confirm(self, f"確定刪除「{selected.title}」?"):
            self.task_delete_requested.emit(selected.identifier)

    def _add_event(self) -> None:
        dialog = _EventEditor(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.event_save_requested.emit(dialog.request())

    def _edit_event(self) -> None:
        selected = self._selected_event()
        if selected is None:
            self.show_error("請先選擇活動")
            return
        dialog = _EventEditor(selected, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.event_save_requested.emit(dialog.request())

    def _delete_event(self) -> None:
        selected = self._selected_event()
        if selected is not None and _confirm(
            self, f"確定刪除「{selected.title}」? 相關待辦會保留。"
        ):
            self.event_delete_requested.emit(selected.identifier)


class _TaskEditor(QDialog):
    def __init__(
        self,
        events: Sequence[EventView],
        value: TaskView | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._identifier = None if value is None else value.identifier
        self.setWindowTitle("新增待辦" if value is None else "編輯待辦")
        self.title_edit = QLineEdit("" if value is None else value.title)
        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        for code, label in (
            ("daily", "每日"),
            ("weekly", "每週"),
            ("event", "活動"),
            ("custom", "自訂"),
        ):
            self.type_combo.addItem(label, code)
        _select_combo(self.type_combo, None if value is None else value.task_type)
        self.due_enabled = QCheckBox("設定到期時間")
        self.due_edit = QDateTimeEdit(QDateTime.currentDateTime().addDays(1))
        self.due_edit.setCalendarPopup(True)
        self.due_enabled.toggled.connect(self.due_edit.setEnabled)
        self.due_enabled.setChecked(value is not None and value.due_at is not None)
        self.due_edit.setEnabled(self.due_enabled.isChecked())
        if value is not None and value.due_at is not None:
            self.due_edit.setDateTime(_qdatetime(value.due_at))
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 9_999)
        self.priority_spin.setValue(100 if value is None else value.priority)
        self.recurrence_combo = QComboBox()
        self.recurrence_combo.addItem("不重複", None)
        self.recurrence_combo.addItem("每日", "daily")
        self.recurrence_combo.addItem("每週", "weekly")
        recurrence: str | None
        if value is not None and value.recurrence == "unknown":
            self.recurrence_combo.addItem("未知規則 (保留, 需人工確認)", "preserve-unknown")
            recurrence = "preserve-unknown"
        else:
            recurrence = None if value is None else _recurrence_frequency(value.recurrence)
        self.recurrence_combo.setCurrentIndex(max(0, self.recurrence_combo.findData(recurrence)))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 365)
        self.interval_spin.setValue(1 if value is None else _recurrence_interval(value.recurrence))
        self.event_combo = QComboBox()
        self.event_combo.addItem("不關聯活動", None)
        for event in events:
            self.event_combo.addItem(event.title, event.identifier)
        if value is not None and value.event_id is not None:
            self.event_combo.setCurrentIndex(max(0, self.event_combo.findData(value.event_id)))
        self.hidden_check = QCheckBox("隱藏")
        self.hidden_check.setChecked(False if value is None else value.hidden)
        self.notes_edit = QTextEdit("" if value is None else value.notes)
        self.notes_edit.setMaximumHeight(90)
        form = QFormLayout()
        form.addRow("標題", self.title_edit)
        form.addRow("類型", self.type_combo)
        form.addRow(self.due_enabled, self.due_edit)
        form.addRow("優先級", self.priority_spin)
        form.addRow("重複", self.recurrence_combo)
        form.addRow("重複間隔", self.interval_spin)
        form.addRow("關聯活動", self.event_combo)
        form.addRow("顯示", self.hidden_check)
        form.addRow("備註", self.notes_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def request(self) -> TaskSaveRequest:
        frequency = self.recurrence_combo.currentData()
        preserve_unknown = frequency == "preserve-unknown"
        return TaskSaveRequest(
            self._identifier,
            self.title_edit.text().strip(),
            _combo_value(self.type_combo),
            _datetime(self.due_edit) if self.due_enabled.isChecked() else None,
            self.priority_spin.value(),
            frequency if frequency in {"daily", "weekly"} else None,
            self.interval_spin.value(),
            self.event_combo.currentData()
            if isinstance(self.event_combo.currentData(), str)
            else None,
            self.hidden_check.isChecked(),
            self.notes_edit.toPlainText().strip(),
            preserve_unknown,
        )

    def _accept_valid(self) -> None:
        try:
            self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "資料不完整", str(exc))
            return
        self.accept()


class _EventEditor(QDialog):
    def __init__(self, value: EventView | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._identifier = None if value is None else value.identifier
        self.setWindowTitle("新增活動" if value is None else "編輯活動")
        self.title_edit = QLineEdit("" if value is None else value.title)
        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        for code, label in (
            ("limited", "限時活動"),
            ("daily", "每日"),
            ("weekly", "每週"),
            ("redeem_code", "兌換碼"),
            ("custom", "自訂"),
        ):
            self.type_combo.addItem(label, code)
        _select_combo(self.type_combo, None if value is None else value.event_type)
        self.start_enabled = QCheckBox("設定開始")
        self.start_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.start_edit.setCalendarPopup(True)
        self.start_enabled.toggled.connect(self.start_edit.setEnabled)
        self.start_enabled.setChecked(value is not None and value.starts_at is not None)
        self.start_edit.setEnabled(self.start_enabled.isChecked())
        self.end_enabled = QCheckBox("設定到期")
        self.end_edit = QDateTimeEdit(QDateTime.currentDateTime().addDays(7))
        self.end_edit.setCalendarPopup(True)
        self.end_enabled.toggled.connect(self.end_edit.setEnabled)
        self.end_enabled.setChecked(value is not None and value.ends_at is not None)
        self.end_edit.setEnabled(self.end_enabled.isChecked())
        if value is not None and value.starts_at is not None:
            self.start_edit.setDateTime(_qdatetime(value.starts_at))
        if value is not None and value.ends_at is not None:
            self.end_edit.setDateTime(_qdatetime(value.ends_at))
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 9_999)
        self.priority_spin.setValue(100 if value is None else value.priority)
        self.status_combo = QComboBox()
        for code, label in (
            ("active", "進行中"),
            ("planned", "未開始"),
            ("completed", "已完成"),
            ("ignored", "忽略"),
        ):
            self.status_combo.addItem(label, code)
        _select_combo(self.status_combo, None if value is None else value.status)
        self.hidden_check = QCheckBox("隱藏")
        self.hidden_check.setChecked(False if value is None else value.hidden)
        self.notes_edit = QTextEdit("" if value is None else value.notes)
        self.notes_edit.setMaximumHeight(90)
        form = QFormLayout()
        form.addRow("標題", self.title_edit)
        form.addRow("類型", self.type_combo)
        form.addRow(self.start_enabled, self.start_edit)
        form.addRow(self.end_enabled, self.end_edit)
        form.addRow("優先級", self.priority_spin)
        form.addRow("狀態", self.status_combo)
        form.addRow("顯示", self.hidden_check)
        form.addRow("備註", self.notes_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def request(self) -> EventSaveRequest:
        return EventSaveRequest(
            self._identifier,
            self.title_edit.text().strip(),
            _combo_value(self.type_combo),
            _datetime(self.start_edit) if self.start_enabled.isChecked() else None,
            _datetime(self.end_edit) if self.end_enabled.isChecked() else None,
            self.priority_spin.value(),
            _combo_value(self.status_combo),
            self.hidden_check.isChecked(),
            self.notes_edit.toPlainText().strip(),
        )

    def _accept_valid(self) -> None:
        try:
            self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "資料不完整", str(exc))
            return
        self.accept()


def _table(name: str, model: QAbstractTableModel) -> QTableView:
    table = QTableView()
    table.setObjectName(name)
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _confirm(parent: QWidget, text: str) -> bool:
    return QMessageBox.question(parent, "確認刪除", text) == QMessageBox.StandardButton.Yes


def _datetime(editor: QDateTimeEdit) -> datetime:
    value = editor.dateTime()
    return datetime(
        value.date().year(),
        value.date().month(),
        value.date().day(),
        value.time().hour(),
        value.time().minute(),
        value.time().second(),
        tzinfo=TAIPEI,
    )


def _qdatetime(value: datetime) -> QDateTime:
    local = value.astimezone(TAIPEI)
    return QDateTime(
        local.year,
        local.month,
        local.day,
        local.hour,
        local.minute,
        local.second,
    )


def _datetime_text(value: datetime | None) -> str:
    return "未設定" if value is None else value.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


def _task_status(value: TaskView) -> str:
    if value.completed_at is not None:
        return "已完成"
    if value.hidden:
        return "已隱藏"
    if value.snoozed_until is not None and value.snoozed_until > datetime.now(UTC):
        return f"延後至 {_datetime_text(value.snoozed_until)}"
    return "待處理"


def _recurrence_text(value: str | None) -> str:
    if value is None:
        return "不重複"
    frequency = _recurrence_frequency(value)
    interval = _recurrence_interval(value)
    unit = "日" if frequency == "daily" else "週"
    return f"每 {interval} {unit}" if frequency is not None else "資料需確認"


def _recurrence_frequency(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.split(":", maxsplit=1)
    return parts[0] if parts[0] in {"daily", "weekly"} else None


def _recurrence_interval(value: str | None) -> int:
    if value is None or ":" not in value:
        return 1
    try:
        return max(1, int(value.split(":", maxsplit=1)[1]))
    except ValueError:
        return 1


def _combo_value(combo: QComboBox) -> str:
    data = combo.currentData()
    if isinstance(data, str) and combo.currentText() == combo.itemText(combo.currentIndex()):
        return data
    value = combo.currentText().strip()
    _required(value, "欄位")
    return value


def _select_combo(combo: QComboBox, value: str | None) -> None:
    if value is None:
        combo.setCurrentIndex(0)
        return
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif combo.isEditable():
        combo.setEditText(value)


__all__ = [
    "DueSummaryView",
    "EventSaveRequest",
    "EventView",
    "SnoozeTaskRequest",
    "TaskSaveRequest",
    "TaskView",
    "TasksEventsPage",
    "TasksEventsSnapshot",
]
