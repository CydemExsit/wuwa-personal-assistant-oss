"""PySide6 bootstrap for the local Windows desktop application.

The bootstrap performs only local storage initialization and desktop-shell setup.
Capture and OCR services are deliberately absent here; they are created later only
after the user explicitly starts recognition from the echo-enhancement page.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import logging
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from wuwa_assistant import __version__
from wuwa_assistant.application import (
    SCORING_TEMPLATE_SETTING_KEY,
    CharacterCultivationService,
    CultivationPlanningService,
    DataManagementService,
    DemoDataService,
    EchoHistoryService,
    GachaPlanningService,
    OnboardingService,
    ScoringProfileService,
    ScoringWorkflowService,
    TasksEventsService,
)
from wuwa_assistant.display import (
    APP_ID,
    APP_TITLE,
    DEFAULT_RESOLUTION,
    DISPLAY_SETUP_KEY,
    RESOLUTION_KEY,
)
from wuwa_assistant.public_data import OfficialPublicDataService
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.scoring.character_guides import load_character_seed_definitions
from wuwa_assistant.storage import (
    AppPaths,
    Database,
    SettingsService,
    StorageError,
    activate_staged_restore,
    create_database,
)
from wuwa_assistant.ui import MainWindow
from wuwa_assistant.ui.controllers import (
    CharactersPresenter,
    DataManagementPresenter,
    EchoWorkspacePresenter,
    EchoWorkspaceSettings,
    GachaPresenter,
    LiveCaptureController,
    OfficialPublicDataPresenter,
    OnboardingController,
    ScoringProfilesPresenter,
    TasksEventsPresenter,
    TodayPlanPresenter,
)
from wuwa_assistant.ui.pages.onboarding import OnboardingWizard
from wuwa_assistant.ui.pages.settings import (
    OCR_CONFIDENCE_KEY,
    SAMPLE_RATE_KEY,
    STABILITY_WAIT_KEY,
)
from wuwa_assistant.ui.settings_store import setting_float
from wuwa_assistant.ui.theme import apply_dark_theme

from .logging_setup import configure_local_logging

_BYTE_ARRAY_TAG = "__wuwa_qbytearray_base64__"
_qt_application: QApplication | None = None


class DatabaseSettingsStore:
    """Adapt JSON-backed application settings to the Qt settings protocol.

    Qt window geometry is a ``QByteArray`` and therefore cannot be written directly
    to a JSON column.  Only that byte-oriented value receives a tagged base64
    representation; normal settings remain ordinary JSON primitives.
    """

    def __init__(self, settings: SettingsService) -> None:
        self._settings = settings

    def get(self, key: str, default: object | None = None) -> object | None:
        value = self._settings.get(key, default)
        return _decode_setting(value, default)

    def set(self, key: str, value: object) -> None:
        self._settings.set(key, _encode_setting(value))

    def sync(self) -> None:
        """Writes are transactionally committed by ``SettingsService.set``."""


def _encode_setting(value: object) -> Any:
    if isinstance(value, QByteArray):
        encoded = base64.b64encode(value.data()).decode("ascii")
        return {_BYTE_ARRAY_TAG: encoded}
    if isinstance(value, (bytes, bytearray)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {_BYTE_ARRAY_TAG: encoded}
    return value


def _decode_setting(value: Any, default: object | None) -> object | None:
    if not isinstance(value, dict) or set(value) != {_BYTE_ARRAY_TAG}:
        return value
    encoded = value.get(_BYTE_ARRAY_TAG)
    if not isinstance(encoded, str):
        return default
    try:
        return QByteArray(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError):
        return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wuwa-assistant-gui",
        description="鳴潮個人養成助理 Windows 本機應用程式",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help="覆寫本機資料根目錄; 省略時使用目前使用者的 LocalAppData",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="初始化資料庫與視窗後立即結束, 不啟動 capture 或 OCR",
    )
    parser.add_argument(
        "--ocr-self-test",
        action="store_true",
        help="只驗證封裝內的本機 RapidOCR 模型可初始化, 不擷取畫面或建立資料庫",
    )
    parser.add_argument("--check-reminders", action="store_true", help="檢查週末提醒後結束")
    parser.add_argument("--reminder-dry-run", action="store_true", help="僅驗證提醒, 不送出通知")
    reminder_setup = parser.add_mutually_exclusive_group()
    reminder_setup.add_argument("--enable-background-reminders", action="store_true")
    reminder_setup.add_argument("--disable-background-reminders", action="store_true")
    parser.add_argument("--reminder-hour", type=int, choices=range(24), default=10)
    parser.add_argument("--reminder-minute", type=int, choices=range(60), default=0)
    return parser


def _application() -> QApplication:
    global _qt_application

    existing = QCoreApplication.instance()
    if existing is not None:
        if not isinstance(existing, QApplication):
            raise RuntimeError("A non-GUI QCoreApplication already exists")
        _qt_application = existing
        return existing

    _qt_application = QApplication(["wuwa-assistant-gui"])
    return _qt_application


def _configure_application(application: QApplication) -> None:
    application.setApplicationName(APP_ID)
    application.setApplicationDisplayName(APP_TITLE)
    application.setOrganizationName(APP_ID)
    application.setApplicationVersion(__version__)
    application.setWindowIcon(QIcon(str(files("wuwa_assistant.ui.styles").joinpath("app.ico"))))
    application.setQuitOnLastWindowClosed(True)
    apply_dark_theme(application)


def _paths(data_root: Path | None) -> AppPaths:
    return AppPaths.from_root(data_root) if data_root is not None else AppPaths.default()


def _run_ocr_self_test() -> int:
    """Force all three bundled OCR models to initialize using a synthetic ROI."""

    try:
        import cv2
        import numpy as np

        from wuwa_assistant.ocr import RapidOcrEngine

        # A synthetic blank crop proves local model discovery and ONNX Runtime
        # initialization without reading a file, monitor, game window, or account data.
        image = np.full((64, 240, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "TEST",
            (20, 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        engine = RapidOcrEngine()
        tokens = engine.recognize(image)
        if not engine.is_initialized or not tokens:
            raise RuntimeError("RapidOCR did not recognize the synthetic test crop")
    except Exception as exc:
        print(f"本機 OCR 自我檢查失敗 ({type(exc).__name__})。", file=sys.stderr)
        return 3
    print("本機 OCR 自我檢查通過; 沒有擷取或保存任何畫面。")
    print("OCR execution providers: " + ", ".join(engine.execution_providers))
    return 0


def _show_storage_error(
    application: QApplication,
    error: StorageError,
    *,
    smoke_test: bool,
) -> None:
    message = f"無法開啟本機資料。\n\n{error}\n\n程式沒有刪除或重設任何既有資料。"
    print(message, file=sys.stderr)
    if not smoke_test:
        QMessageBox.critical(None, "鳴潮個人養成助理 — 本機資料錯誤", message)
        application.processEvents()


def _create_window(
    database: Database,
    *,
    enable_live_services: bool,
    data_set_id: str | None = None,
    active_demo: bool = False,
) -> MainWindow:
    settings_store = DatabaseSettingsStore(database.settings)
    window = MainWindow(settings=settings_store, data_root=database.paths.root)
    if not enable_live_services:
        return window

    workspace_settings = EchoWorkspaceSettings(
        sample_hz=setting_float(database.settings.get(SAMPLE_RATE_KEY), 5.0),
        stable_duration_seconds=setting_float(database.settings.get(STABILITY_WAIT_KEY), 0.60),
        confidence_threshold=setting_float(database.settings.get(OCR_CONFIDENCE_KEY), 0.85),
        resolution=str(database.settings.get(RESOLUTION_KEY, DEFAULT_RESOLUTION)),
    )
    live_controller = LiveCaptureController(parent=window)
    history_service = EchoHistoryService(database.session_factory)
    selected_data_set_id = data_set_id or history_service.ensure_active_primary_dataset()
    profile_service = ScoringProfileService(database.session_factory)
    profile_service.ensure_system_templates(
        load_template_seed_definitions() + load_character_seed_definitions()
    )
    preferred_template_kind = str(
        database.settings.get(SCORING_TEMPLATE_SETTING_KEY, "generic_dps") or "generic_dps"
    )
    preferred_profile = next(
        (
            profile
            for profile in profile_service.list_profiles(data_set_id=selected_data_set_id)
            if profile.template_kind == preferred_template_kind
        ),
        None,
    )
    existing_default = database.settings.get(
        f"scoring/default_profile/{selected_data_set_id}",
        None,
    )
    if preferred_profile is not None and existing_default is None:
        profile_service.set_default(preferred_profile.id, data_set_id=selected_data_set_id)
    scoring_workflow = ScoringWorkflowService(database.session_factory, profile_service)
    profiles_presenter = ScoringProfilesPresenter(
        service=profile_service,
        page=window.scoring_profiles_page,
        data_set_id=selected_data_set_id,
        parent=window,
    )
    from wuwa_assistant.application.replacement_training import ReplacementTrainingService
    from wuwa_assistant.ui.navigation import PageId

    presenter = EchoWorkspacePresenter(
        live_controller=live_controller,
        history_service=history_service,
        echo_page=window.echo_page,
        history_page=window.history_page,
        settings_page=window.settings_page,
        scoring_workflow=scoring_workflow,
        replacement_service=ReplacementTrainingService(database.session_factory),
        scoring_preview_page=window.scoring_profiles_page,
        data_set_id=selected_data_set_id,
        preferred_monitor_id=str(database.settings.get("capture/monitor_id", "") or ""),
        app_version=__version__,
        settings=workspace_settings,
        parent=window,
    )
    characters_presenter = CharactersPresenter(
        service=CharacterCultivationService(database.session_factory),
        scoring_profiles=profile_service,
        page=window.characters_page,
        data_set_id=selected_data_set_id,
        parent=window,
    )
    from wuwa_assistant.application.tacet_sync import TacetCatalogSync

    planning_presenter = TodayPlanPresenter(
        planning_service=CultivationPlanningService(database.session_factory),
        character_service=CharacterCultivationService(database.session_factory),
        scoring_profiles=profile_service,
        settings=database.settings,
        page=window.today_plan_page,
        tacet_cache_directory=database.paths.public_data_cache_dir,
        data_set_id=selected_data_set_id,
        parent=window,
    )
    from wuwa_assistant.public_data.schedule_state import ScheduleCompletionService

    tasks_presenter = TasksEventsPresenter(
        service=TasksEventsService(database.session_factory),
        page=window.tasks_events_page,
        data_set_id=selected_data_set_id,
        completion_service=ScheduleCompletionService(database.settings),
        parent=window,
    )
    gacha_presenter = GachaPresenter(
        service=GachaPlanningService(database.session_factory),
        page=window.gacha_page,
        data_set_id=selected_data_set_id,
        parent=window,
    )
    data_presenter = DataManagementPresenter(
        service=DataManagementService(engine=database.engine, paths=database.paths),
        demo_service=DemoDataService(database.session_factory),
        page=window.settings_page,
        active_demo=active_demo,
        parent=window,
    )
    from wuwa_assistant.public_data.redeem_service import RedeemCodeService
    from wuwa_assistant.ui.controllers.redeem_codes import RedeemCodesPresenter

    redeem_presenter = RedeemCodesPresenter(
        RedeemCodeService(database.paths.public_data_cache_dir),
        window,
        DatabaseSettingsStore(database.settings),
    )
    window.register_shutdown(
        redeem_presenter.finish_shutdown, prepare=redeem_presenter.prepare_shutdown
    )
    public_data_presenter = OfficialPublicDataPresenter(
        service=OfficialPublicDataService(database.paths.public_data_cache_dir),
        settings_page=window.settings_page,
        tasks_page=window.tasks_events_page,
        gacha_page=window.gacha_page,
        auto_update=lambda: database.settings.get("public_data/auto_update", False) is True,
        supplementary_sync=TacetCatalogSync(database.paths.public_data_cache_dir).synchronize,
        parent=window,
    )
    public_data_presenter.feed_changed.connect(
        lambda _feed: window.today_plan_page.set_activity_notices(
            public_data_presenter.activity_notices()
        )
    )
    # Cached catalog loading is cheap; skip all legacy material planning writes.
    public_data_presenter.feed_changed.connect(lambda _feed: planning_presenter.refresh())
    from wuwa_assistant.ui.controllers.background_reminders import BackgroundRemindersPresenter

    background_reminders = BackgroundRemindersPresenter(
        settings=database.settings,
        page=window.settings_page,
        data_root=database.paths.root,
        parent=window,
    )
    profiles_presenter.profile_choices_changed.connect(window.echo_page.set_scoring_profiles)
    profiles_presenter.profile_changed.connect(presenter.rescore_current)
    characters_presenter.scoring_context_changed.connect(presenter.refresh_scoring_context)
    characters_presenter.character_choices_changed.connect(window.echo_page.set_characters)
    window.characters_page.training_requested.connect(presenter.set_training_target)
    presenter.training_ready.connect(lambda: window.navigate_to(PageId.ECHO_ENHANCEMENT))
    presenter.characters_changed.connect(characters_presenter.refresh_external_changes)
    characters_presenter.dashboard_snapshot_changed.connect(
        window.dashboard_page.set_character_snapshot
    )
    characters_presenter.dashboard_snapshot_changed.connect(
        lambda _snapshot: planning_presenter.refresh()
    )
    planning_presenter.dashboard_priority_changed.connect(window.dashboard_page.set_today_priority)
    tasks_presenter.dashboard_due_changed.connect(window.dashboard_page.set_due_tasks)
    live_controller.running_changed.connect(window.scoring_profiles_page.set_capture_active)
    window.settings_page.settings_saved.connect(presenter.update_settings)
    from wuwa_assistant.ui.controllers.display_mode import DisplayModeController

    display_controller = DisplayModeController(
        presenter=presenter,
        page=window.echo_page,
        settings_page=window.settings_page,
        settings=settings_store,
        parent=window,
    )
    window.register_shutdown(display_controller.shutdown)
    data_presenter.restart_required.connect(lambda _message: window.close())
    window.register_shutdown(
        profiles_presenter.finish_shutdown, prepare=profiles_presenter.prepare_shutdown
    )
    window.register_shutdown(
        characters_presenter.finish_shutdown, prepare=characters_presenter.prepare_shutdown
    )
    window.register_shutdown(
        planning_presenter.finish_shutdown, prepare=planning_presenter.prepare_shutdown
    )
    window.register_shutdown(
        tasks_presenter.finish_shutdown, prepare=tasks_presenter.prepare_shutdown
    )
    window.register_shutdown(
        gacha_presenter.finish_shutdown, prepare=gacha_presenter.prepare_shutdown
    )
    window.register_shutdown(
        data_presenter.finish_shutdown, prepare=data_presenter.prepare_shutdown
    )
    window.register_shutdown(
        public_data_presenter.finish_shutdown, prepare=public_data_presenter.prepare_shutdown
    )
    window.register_shutdown(
        background_reminders.finish_shutdown, prepare=background_reminders.prepare_shutdown
    )
    window.register_shutdown(presenter.finish_shutdown, prepare=presenter.prepare_shutdown)
    profiles_presenter.initialize()
    characters_presenter.initialize()
    planning_presenter.initialize()
    tasks_presenter.initialize()
    gacha_presenter.initialize()
    public_data_presenter.initialize()
    redeem_presenter.initialize()
    presenter.initialize()
    return window


def _run_onboarding(database: Database) -> bool:
    """Run first-launch setup before any continuous capture service exists."""

    service = OnboardingService(
        database.session_factory,
        database.settings,
        data_path=database.paths.root,
    )
    if service.load_state().completed and database.settings.get(DISPLAY_SETUP_KEY, False) is True:
        return True
    wizard = OnboardingWizard(data_path=database.paths.root)
    controller = OnboardingController(service=service, wizard=wizard, parent=wizard)
    controller.initialize()
    try:
        return wizard.exec() == QDialog.DialogCode.Accepted
    finally:
        controller.shutdown()
        wizard.deleteLater()


def _selected_data_set_id(database: Database) -> str:
    """Use the onboarding partition while it exists, otherwise primary data."""

    state = OnboardingService(
        database.session_factory,
        database.settings,
        data_path=database.paths.root,
    ).load_state()
    if state.completed and state.data_set_id:
        from wuwa_assistant.storage.orm import DataSetRecord

        with database.session_factory() as session:
            if session.get(DataSetRecord, state.data_set_id) is not None:
                return state.data_set_id
    return EchoHistoryService(database.session_factory).ensure_active_primary_dataset()


def _is_demo_data_set(database: Database, data_set_id: str) -> bool:
    from wuwa_assistant.storage.orm import DataSetRecord

    with database.session_factory() as session:
        record = session.get(DataSetRecord, data_set_id)
        return record is not None and record.kind == "demo"


def main(argv: list[str] | None = None) -> int:
    """Initialize local storage and run the ordinary resizable desktop window."""

    args = _build_parser().parse_args(argv)
    if args.ocr_self_test:
        return _run_ocr_self_test()
    if args.check_reminders or args.reminder_dry_run:
        from wuwa_assistant.reminders.background import run_background_reminders

        return run_background_reminders(_paths(args.data_root), dry_run=args.reminder_dry_run)
    if args.enable_background_reminders or args.disable_background_reminders:
        from wuwa_assistant.reminders.background import ENABLED_KEY, HOUR_KEY, MINUTE_KEY
        from wuwa_assistant.reminders.windows_task import configure_task

        reminder_paths = _paths(args.data_root)
        enabled = args.enable_background_reminders
        try:
            configure_task(
                reminder_paths.root,
                enabled=enabled,
                hour=args.reminder_hour,
                minute=args.reminder_minute,
            )
            with create_database(reminder_paths) as reminder_database:
                reminder_database.settings.set(ENABLED_KEY, enabled)
                reminder_database.settings.set(HOUR_KEY, args.reminder_hour)
                reminder_database.settings.set(MINUTE_KEY, args.reminder_minute)
            return 0
        except (OSError, ValueError, RuntimeError):
            logging.getLogger(__name__).exception("background_reminder_configuration_failed")
            return 2
    application = _application()
    _configure_application(application)

    database: Database | None = None
    window: MainWindow | None = None
    logger: logging.Logger | None = None
    from wuwa_assistant.ui.controllers.software_update import SoftwareUpdatePresenter

    software_update: SoftwareUpdatePresenter | None = None
    try:
        paths = _paths(args.data_root)
        logger = configure_local_logging(paths.logs_dir, app_version=__version__)
        restore = activate_staged_restore(paths)
        if restore is not None:
            logger.info(
                "staged_restore_activated schema=%s rollback_created=true", restore.schema_revision
            )
        database = create_database(paths)
        logger.info("database_ready schema=%s", database.health.schema_revision or "unknown")
        if not args.smoke_test and not _run_onboarding(database):
            logger.info("onboarding_cancelled")
            return 0
        selected_data_set_id = None if args.smoke_test else _selected_data_set_id(database)
        window = _create_window(
            database,
            enable_live_services=not args.smoke_test,
            data_set_id=selected_data_set_id,
            active_demo=(
                False
                if selected_data_set_id is None
                else _is_demo_data_set(database, selected_data_set_id)
            ),
        )

        if args.smoke_test:
            # Construction exercises migrations, settings reads, theme resources,
            # navigation models, and the complete top-level widget tree.  It does
            # not show a window or enter the event loop.
            application.processEvents()
            window.close()
            application.processEvents()
            return 0

        window.show()
        software_update = SoftwareUpdatePresenter(
            window.settings_page.software_update,
            window,
            DatabaseSettingsStore(database.settings),
            paths.root / "cache" / "software-updates",
        )
        software_update.available.connect(
            lambda version: window.statusBar().showMessage(
                f"有新版 {version}, 可到「設定 → 程式更新」下載。"
            )
        )
        window.register_shutdown(
            software_update.finish_shutdown, prepare=software_update.prepare_shutdown
        )
        return application.exec()
    except StorageError as exc:
        if logger is not None:
            logger.error("storage_startup_failed type=%s", type(exc).__name__)
        _show_storage_error(application, exc, smoke_test=args.smoke_test)
        return 2
    finally:
        # Explicit QApplication.quit or startup errors can bypass closeEvent.
        # Join outstanding services before destroying their QObjects or engine.
        resources_stopped = window is None or window.finish_shutdown()
        if resources_stopped:
            if window is not None:
                window.deleteLater()
            if database is not None:
                database.dispose()
            if software_update is not None and software_update.pending is not None:
                from wuwa_assistant.updates import UpdateError, launch_installer

                try:
                    launch_installer(software_update.pending, paths.root)
                except (UpdateError, OSError):
                    QMessageBox.warning(
                        None,
                        "更新尚未安裝",
                        "無法啟動更新, 原版本與資料都已保留。"
                        "請重新開啟工具後重試, 或直接執行收到的安裝檔。",
                    )
        elif logger is not None:
            logger.error("shutdown_incomplete resources_retained=true")
        if logger is not None:
            logger.info("application_stopped")


if __name__ == "__main__":
    raise SystemExit(main())
