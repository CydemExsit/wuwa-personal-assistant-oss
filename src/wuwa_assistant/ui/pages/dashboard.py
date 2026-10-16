"""Initial dashboard with safe status and clear next actions."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..navigation import PageId


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    character_progress: str = "尚未建立角色"
    qualified_echoes: str = "尚無紀錄"
    today_priority: str = "先建立第一名常用角色"
    due_tasks: str = "今日無到期項目"
    has_character_data: bool = False
    has_echo_data: bool = False


class _MetricCard(QFrame):
    def __init__(self, title: str, value: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self._value_label = QLabel(value)
        self._value_label.setObjectName(object_name)
        self._value_label.setWordWrap(True)

        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        layout.addWidget(title_label)
        layout.addWidget(self._value_label)
        layout.addStretch(1)

    def set_value(self, value: str) -> None:
        self._value_label.setText(value)


class DashboardPage(QWidget):
    navigate_requested = Signal(object)

    def __init__(
        self,
        snapshot: DashboardSnapshot | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DashboardPage")

        title = QLabel("總覽")
        title.setObjectName("PageTitle")
        subtitle = QLabel("今天的養成狀態與下一步集中顯示在這裡。")
        subtitle.setObjectName("PageDescription")

        safety_card = QFrame()
        safety_card.setObjectName("SafetyCard")
        safety_layout = QVBoxLayout(safety_card)
        safety_title = QLabel("安全模式待命中")
        safety_title.setObjectName("SafetyTitle")
        safety_text = QLabel(
            "目前未擷取畫面。只有在聲骸強化頁手動按下「開始辨識」後,工具才會被動讀取指定顯示器。"
        )
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)

        self.quick_start_card = QFrame()
        self.quick_start_card.setObjectName("DashboardQuickStartCard")
        quick_title = QLabel("先完成第一次聲骸辨識")
        quick_title.setObjectName("DashboardQuickStartTitle")
        quick_steps = QLabel(
            "1. 在遊戲中打開任一顆聲骸的強化頁\n"
            "2. 回到本工具確認遊戲所在的左側/右側螢幕\n"
            "3. 按下「開始聲骸辨識」, 之後照常在遊戲內操作"
        )
        quick_steps.setObjectName("DashboardQuickStartSteps")
        quick_steps.setWordWrap(True)
        self.quick_start_button = QPushButton("開始聲骸辨識")
        self.quick_start_button.setObjectName("DashboardEchoButton")
        self.quick_start_button.setProperty("role", "primary")
        self.quick_start_button.clicked.connect(
            lambda: self.navigate_requested.emit(PageId.ECHO_ENHANCEMENT)
        )
        quick_layout = QVBoxLayout(self.quick_start_card)
        quick_layout.setContentsMargins(24, 22, 24, 22)
        quick_layout.setSpacing(14)
        quick_layout.addWidget(quick_title)
        quick_layout.addWidget(quick_steps)
        quick_layout.addWidget(self.quick_start_button, 0)

        self._character_card = _MetricCard(
            "角色聲骸評分", "尚未建立角色", "DashboardCharacterProgress"
        )
        self._echo_card = _MetricCard("聲骸及格件數", "尚無紀錄", "DashboardQualifiedEchoes")
        self._priority_card = _MetricCard(
            "今日第一優先", "先建立第一名常用角色", "DashboardTodayPriority"
        )
        self._tasks_card = _MetricCard("到期事項", "今日無到期項目", "DashboardDueTasks")
        for card, label, destination in (
            (self._character_card, "查看角色", PageId.CHARACTERS),
            (self._echo_card, "檢視聲骸", PageId.EQUIPMENT),
            (self._priority_card, "開啟養成規劃", PageId.TODAY_PLAN),
            (self._tasks_card, "查看活動與待辦", PageId.TASKS_EVENTS),
        ):
            button = QPushButton(label)
            button.setProperty("role", "quiet")
            button.clicked.connect(
                lambda _checked=False, target=destination: self.navigate_requested.emit(target)
            )
            card_layout = card.layout()
            assert card_layout is not None
            card_layout.addWidget(button)

        metrics = QGridLayout()
        metrics.setSpacing(16)
        metrics.addWidget(self._character_card, 0, 0)
        metrics.addWidget(self._echo_card, 0, 1)
        metrics.addWidget(self._priority_card, 1, 0)
        metrics.addWidget(self._tasks_card, 1, 1)
        metrics.setColumnStretch(0, 1)
        metrics.setColumnStretch(1, 1)
        self.metrics_widget = QWidget()
        self.metrics_widget.setLayout(metrics)

        echo_button = QPushButton("前往聲骸強化")
        echo_button.setObjectName("DashboardEchoSecondaryButton")
        echo_button.clicked.connect(lambda: self.navigate_requested.emit(PageId.ECHO_ENHANCEMENT))
        character_button = QPushButton("管理角色")
        character_button.setObjectName("DashboardCharacterButton")
        character_button.clicked.connect(lambda: self.navigate_requested.emit(PageId.CHARACTERS))
        actions = QHBoxLayout()
        actions.addWidget(echo_button)
        actions.addWidget(character_button)
        actions.addStretch(1)
        self.regular_actions = QWidget()
        self.regular_actions.setLayout(actions)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(40, 36, 40, 36)
        layout.setSpacing(20)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(safety_card)
        layout.addWidget(self.quick_start_card)
        layout.addWidget(self.metrics_widget)
        layout.addWidget(self.regular_actions)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("DashboardScrollArea")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.set_snapshot(snapshot or DashboardSnapshot())

    def set_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._character_card.set_value(snapshot.character_progress)
        self._echo_card.set_value(snapshot.qualified_echoes)
        self._priority_card.set_value(snapshot.today_priority)
        self._tasks_card.set_value(snapshot.due_tasks)
        self._set_empty_state(not snapshot.has_echo_data)

    def set_character_snapshot(self, snapshot: DashboardSnapshot) -> None:
        """Update character-owned metrics without overwriting other presenters.

        Dashboard sections are fed by independent background presenters.  Keeping
        the updates granular prevents a character refresh from racing with the
        daily planner or task presenter and restoring their placeholder values.
        """

        self._character_card.set_value(snapshot.character_progress)
        self._echo_card.set_value(snapshot.qualified_echoes)
        self._set_empty_state(not snapshot.has_echo_data)

    def set_today_priority(self, value: str) -> None:
        self._priority_card.set_value(value)

    def set_due_tasks(self, value: str) -> None:
        self._tasks_card.set_value(value)

    def _set_empty_state(self, empty: bool) -> None:
        self.quick_start_card.setVisible(empty)
        self.metrics_widget.setVisible(not empty)
        self.regular_actions.setVisible(not empty)
