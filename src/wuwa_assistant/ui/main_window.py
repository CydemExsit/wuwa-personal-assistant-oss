"""Resizable, second-monitor-friendly desktop application shell."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QObject, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QMouseEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.display import APP_TITLE

from .navigation import NavigationModel, PageId
from .pages import (
    CharactersPage,
    DashboardPage,
    DashboardSnapshot,
    EchoHistoryPage,
    EchoLivePage,
    GachaPage,
    MonitorOption,
    ScoringProfilesPage,
    SettingsPage,
    TasksEventsPage,
    TodayPlanPage,
)
from .pages.redeem_codes import RedeemCodesPage
from .settings_store import SettingsStore, coerce_settings_store, setting_str
from .shutdown import ShutdownCoordinator
from .theme import load_dark_stylesheet
from .window_state import LAST_PAGE_KEY, WindowStatePersistence


@dataclass(frozen=True)
class _NavigationState:
    page: PageId
    equipment_character: str | None = None
    equipment_slot: int | None = None


class _WorkspaceStack(QStackedWidget):
    """Let the active page determine overflow, independently of hidden pages."""

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def heightForWidth(self, width: int) -> int:
        page = self.currentWidget()
        return page.heightForWidth(width) if page is not None else super().heightForWidth(width)


class MainWindow(QMainWindow):
    """Main application window with data-driven navigation and injectable settings."""

    page_changed = Signal(object)

    def __init__(
        self,
        settings: SettingsStore | QSettings | None = None,
        *,
        monitors: Sequence[MonitorOption] = (),
        data_root: str | Path | None = None,
        dashboard_snapshot: DashboardSnapshot | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MainWindow")
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(800, 480)
        self.resize(1440, 900)
        self.setStyleSheet(load_dark_stylesheet())

        self._history: list[_NavigationState] = []
        self._navigation_state: _NavigationState | None = None
        self._restoring_navigation = False
        self._route_requested = False
        self._settings = coerce_settings_store(settings)
        self._shutdown = ShutdownCoordinator(self)
        self._shutdown.finished.connect(self._shutdown_finished)
        self._shutdown_started = False
        self._shutdown_complete = False
        self._window_state = WindowStatePersistence(self._settings)
        self.navigation_model = NavigationModel(parent=self)
        self.navigation_view = QListView()
        self.navigation_view.setObjectName("NavigationView")
        self.navigation_view.setModel(self.navigation_model)
        self.navigation_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.navigation_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.navigation_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        sidebar = self._build_sidebar()
        self.page_stack = _WorkspaceStack()
        self.page_stack.setObjectName("PageStack")
        self.page_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.dashboard_page = DashboardPage(dashboard_snapshot)
        self.today_plan_page = TodayPlanPage()
        self.characters_page = CharactersPage()
        self.characters_page.equipment_workspace.setContentsMargins(18, 14, 18, 14)
        self.echo_page = EchoLivePage()
        self.gacha_page = GachaPage()
        self.tasks_events_page = TasksEventsPage()
        self.redeem_codes_page = RedeemCodesPage()
        self.history_page = EchoHistoryPage()
        self.settings_page = SettingsPage(
            self._settings,
            monitors=monitors,
            data_root=data_root,
        )
        self.scoring_profiles_page = ScoringProfilesPage()
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setObjectName("SettingsTabs")
        self.settings_tabs.addTab(self.settings_page, "一般設定")
        self.settings_tabs.addTab(self.scoring_profiles_page, "評分設定檔")
        self.settings_tabs.addTab(self.history_page, "歷史紀錄")
        self._pages: dict[PageId, QWidget] = {
            PageId.DASHBOARD: self.dashboard_page,
            PageId.TODAY_PLAN: self.today_plan_page,
            PageId.CHARACTERS: self.characters_page,
            PageId.EQUIPMENT: self.characters_page.equipment_workspace,
            PageId.ECHO_ENHANCEMENT: self.echo_page,
            PageId.GACHA: self.gacha_page,
            PageId.TASKS_EVENTS: self.tasks_events_page,
            PageId.REDEEM_CODES: self.redeem_codes_page,
            PageId.SETTINGS: self.settings_tabs,
        }
        for page in self._pages.values():
            self.page_stack.addWidget(page)

        central = QWidget()
        central.setObjectName("ApplicationSurface")
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        body = QFrame()
        body.setObjectName("ApplicationBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(sidebar)
        # Keep every existing workspace reachable on a small logical desktop
        # (for example, 1080p at 200%). Hidden pages can have a larger minimum
        # size than the screen, so the outer viewport provides a fallback.
        self.workspace_scroll = QScrollArea()
        self.workspace_scroll.setObjectName("WorkspaceScrollArea")
        self.workspace_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setWidget(self.page_stack)
        row.addWidget(self.workspace_scroll, 1)
        body_layout.addLayout(row)
        self.redeem_banner = QFrame()
        self.redeem_banner.setObjectName("SafetyCard")
        banner_layout = QHBoxLayout(self.redeem_banner)
        self.redeem_notice = QLabel("")
        self.redeem_notice.setWordWrap(True)
        banner_layout.addWidget(self.redeem_notice, 1)
        self.redeem_view_button = QPushButton("查看兌換碼")
        self.redeem_view_button.clicked.connect(lambda: self.navigate_to(PageId.REDEEM_CODES))
        banner_layout.addWidget(self.redeem_view_button)
        self.redeem_mute_button = QPushButton("本次啟動不再提醒")
        self.redeem_mute_button.clicked.connect(self.redeem_codes_page.session_mute_requested.emit)
        banner_layout.addWidget(self.redeem_mute_button)
        self.redeem_dismiss_button = QPushButton("知道了")
        self.redeem_dismiss_button.clicked.connect(self.redeem_codes_page.acknowledged.emit)
        banner_layout.addWidget(self.redeem_dismiss_button)
        self.redeem_banner.hide()
        central_layout.addWidget(self.redeem_banner)
        central_layout.addWidget(body)
        self.setCentralWidget(central)

        self.statusBar().setObjectName("ApplicationStatusBar")
        self.statusBar().showMessage("Capture 未啟用 · 本機離線模式")
        self._shutdown_retry = QPushButton("重試安全關閉")
        self._shutdown_retry.setObjectName("ShutdownRetryButton")
        self._shutdown_retry.clicked.connect(self._start_shutdown)
        self.statusBar().addPermanentWidget(self._shutdown_retry)
        self._shutdown_retry.hide()

        selection_model = self.navigation_view.selectionModel()
        if selection_model is not None:
            selection_model.currentChanged.connect(self._on_navigation_changed)
        self.navigation_view.clicked.connect(self._on_navigation_clicked)
        self.dashboard_page.navigate_requested.connect(self._handle_dashboard_navigation)
        self.characters_page.equipment_requested.connect(self._handle_equipment_navigation)
        self.characters_page.cultivation_requested.connect(
            lambda: self.navigate_to(PageId.CHARACTERS)
        )
        self.back_shortcut = QShortcut(QKeySequence.StandardKey.Back, self)
        self.back_shortcut.activated.connect(self.go_back)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

        self.geometry_restored = self._window_state.restore(self)
        if not self.geometry_restored:
            self.resize(1440, 900)
        self._fit_initial_geometry()
        saved_page = setting_str(self._settings.get(LAST_PAGE_KEY), PageId.DASHBOARD.value)
        try:
            initial_page = PageId(saved_page)
        except ValueError:
            initial_page = PageId.DASHBOARD
        self.navigate_to(initial_page)

    def _fit_initial_geometry(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry().adjusted(16, 32, -16, -16)
        self.setMinimumSize(min(800, available.width()), min(480, available.height()))
        self.resize(min(self.width(), available.width()), min(self.height(), available.height()))
        if self.geometry_restored:
            self.move(
                max(available.left(), min(self.x(), available.right() - self.width() + 1)),
                max(available.top(), min(self.y(), available.bottom() - self.height() + 1)),
            )
        else:
            self.move(available.center() - self.rect().center())

    @property
    def current_page_id(self) -> PageId:
        page = self.page_stack.currentWidget()
        for page_id, candidate in self._pages.items():
            if candidate is page:
                return page_id
        return PageId.DASHBOARD

    def page_widget(self, page_id: PageId) -> QWidget:
        if page_id is PageId.HISTORY:
            return self.history_page
        return self._pages[page_id]

    def register_shutdown(
        self,
        callback: Callable[[], object],
        *,
        prepare: Callable[[], object] | None = None,
    ) -> None:
        """Register a worker drain plus an optional, nonblocking GUI preparation.

        A callback without ``prepare`` keeps the older GUI-thread behavior.
        Callbacks with preparation must perform no Qt operations in their drain.
        """

        self._shutdown.register(callback, prepare=prepare)

    def finish_shutdown(self) -> bool:
        """Drain services if the application event loop exited unexpectedly."""

        return self._shutdown.finish_blocking()

    def navigate_to(self, page_id: PageId) -> None:
        if self._shutdown_started:
            return
        if page_id is PageId.HISTORY:
            self.settings_tabs.setCurrentWidget(self.history_page)
            page_id = PageId.SETTINGS
        index = self.navigation_model.index_for(page_id)
        if not index.isValid():
            return
        self._remember_departing_view()
        if page_id is PageId.EQUIPMENT and not self._route_requested:
            self.characters_page.equipment_workspace.setCurrentIndex(0)
        self.navigation_view.setCurrentIndex(index)
        self.page_stack.setCurrentWidget(self._pages[page_id])
        self.page_stack.updateGeometry()
        self.workspace_scroll.horizontalScrollBar().setValue(0)
        self.workspace_scroll.verticalScrollBar().setValue(0)
        self._settings.set(LAST_PAGE_KEY, page_id.value)
        self._record_navigation()

    def _remember_departing_view(self) -> None:
        if not self._restoring_navigation and not self._route_requested:
            self._navigation_state = self._capture_navigation() if self._navigation_state else None

    def _capture_navigation(self) -> _NavigationState:
        page = self.current_page_id
        character = None
        slot = None
        if (
            page is PageId.EQUIPMENT
            and self.characters_page.equipment_workspace.currentIndex() == 1
        ):
            detail_page = self.characters_page.equipment_detail_page
            detail = detail_page._detail
            character = None if detail is None else detail.identifier
            slot = getattr(detail_page, "_focused_slot", None)
        return _NavigationState(page, character, slot)

    def _record_navigation(self) -> None:
        current = self._capture_navigation()
        if not self._restoring_navigation and current != self._navigation_state:
            if self._navigation_state is not None:
                self._history.append(self._navigation_state)
                self._history = self._history[-100:]
            self._navigation_state = current
        self.back_button.setEnabled(bool(self._history))

    def _handle_equipment_navigation(self) -> None:
        previous_route = self._route_requested
        self._route_requested = True
        try:
            self.navigate_to(PageId.EQUIPMENT)
        finally:
            self._route_requested = previous_route

    def go_back(self) -> None:
        """Return through workspace routes without changing editor drafts or capture state."""
        if self._shutdown_started or not self._history:
            return
        state = self._history.pop()
        self._restoring_navigation = True
        self._route_requested = True
        try:
            if state.page is PageId.EQUIPMENT:
                if state.equipment_character in self.characters_page._equipment_details:
                    assert state.equipment_character is not None
                    if state.equipment_slot is not None:
                        self.characters_page.open_equipment_piece(
                            state.equipment_character, state.equipment_slot
                        )
                    else:
                        self.characters_page.open_equipment_character(state.equipment_character)
                else:
                    self.characters_page.equipment_workspace.setCurrentIndex(0)
            self.navigate_to(state.page)
            self._navigation_state = self._capture_navigation()
        finally:
            self._restoring_navigation = False
            self._route_requested = False
        self.back_button.setEnabled(bool(self._history))

    def _owns_widget(self, widget: QWidget) -> bool:
        current: QWidget | None = widget
        while current is not None:
            if current is self:
                return True
            current = current.parentWidget()
        return False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QWidget)
            and self._owns_widget(watched)
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.BackButton
            and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
        ):
            if event.type() == QEvent.Type.MouseButtonRelease:
                dialog = watched.window()
                if isinstance(dialog, QDialog):
                    dialog.reject()
                else:
                    self.go_back()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown.has_actions and not self._shutdown_complete:
            event.ignore()
            self._start_shutdown()
            return
        if not self._shutdown_complete:
            self._save_window_state()
        super().closeEvent(event)

    def _save_window_state(self) -> None:
        self._window_state.save(self)
        self._settings.set(LAST_PAGE_KEY, self.current_page_id.value)
        self._settings.sync()

    def _start_shutdown(self) -> None:
        if self._shutdown.running:
            return
        self._shutdown_started = True
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)
        self.back_shortcut.setEnabled(False)
        self._shutdown_retry.hide()
        self.statusBar().showMessage("正在安全關閉: 停止辨識並保存已提交的變更…")
        self._shutdown.start()

    def _shutdown_finished(self, success: bool) -> None:
        if success:
            try:
                self._save_window_state()
            except Exception:
                logging.getLogger(__name__).exception("shutdown_settings_save_failed")
                success = False
        if success:
            self._shutdown_complete = True
            self.close()
        else:
            self.statusBar().showMessage("安全關閉尚未完成。請重試; 本機資料仍保持開啟。")
            self._shutdown_retry.show()

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("NavigationSidebar")
        sidebar.setFixedWidth(190)

        product_name = QLabel("鳴潮\n個人養成助理")
        product_name.setObjectName("ProductName")
        product_name.setWordWrap(True)
        mode = QLabel("本機離線優先")
        mode.setObjectName("ProductMode")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(10)
        layout.addWidget(product_name)
        layout.addWidget(mode)
        self.back_button = QPushButton("← 返回")
        self.back_button.setObjectName("NavigationBackButton")
        self.back_button.setToolTip("返回上一頁 · 滑鼠側鍵四 / Alt+←")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.go_back)
        layout.addWidget(self.back_button)
        layout.addSpacing(6)
        layout.addWidget(self.navigation_view, 1)

        safety = QLabel("被動讀取畫面 · 不操作遊戲")
        safety.setObjectName("SidebarSafety")
        safety.setWordWrap(True)
        layout.addWidget(safety)
        return sidebar

    def _on_navigation_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        del previous
        page_id = self.navigation_model.page_id_at(current)
        if page_id is None:
            return
        self._remember_departing_view()
        if page_id is PageId.EQUIPMENT and not self._route_requested:
            self.characters_page.equipment_workspace.setCurrentIndex(0)
        self.page_stack.setCurrentWidget(self._pages[page_id])
        self._settings.set(LAST_PAGE_KEY, page_id.value)
        self._record_navigation()
        self.page_changed.emit(page_id)

    def _on_navigation_clicked(self, index: QModelIndex) -> None:
        if self.navigation_model.page_id_at(index) is PageId.EQUIPMENT:
            self.navigate_to(PageId.EQUIPMENT)

    def _handle_dashboard_navigation(self, page_id: object) -> None:
        if isinstance(page_id, PageId):
            self.navigate_to(page_id)
