"""Qt-safe controller for explicit local backup, restore, and demo-data actions."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from wuwa_assistant.application.data_management import (
    DataManagementService,
    DiagnosticBundleReceipt,
    DiagnosticPreview,
    ExportReceipt,
    ImportPreview,
    StagedRestore,
)
from wuwa_assistant.application.onboarding import DemoDataService, DemoDataSnapshot
from wuwa_assistant.storage.backup import SqliteSnapshotInfo
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.pages.settings import SettingsPage

type ImportFileChooser = Callable[[], Path | None]
type ConfirmationDialog = Callable[[str, str], bool]


class _Operation(StrEnum):
    BACKUP = "backup"
    EXPORT = "export"
    IMPORT_PREVIEW = "import_preview"
    IMPORT_STAGE = "import_stage"
    DIAGNOSTIC_PREVIEW = "diagnostic_preview"
    DIAGNOSTIC_CREATE = "diagnostic_create"
    DEMO_CREATE = "demo_create"
    DEMO_CLEAR = "demo_clear"


@dataclass(frozen=True, slots=True)
class _Result:
    operation: _Operation
    payload: object | None = None
    error: str | None = None


class DataManagementPresenter(QObject):
    """Run disk and database work off the GUI thread after explicit user intents."""

    _job_completed = Signal(object)
    restart_required = Signal(str)

    def __init__(
        self,
        *,
        service: DataManagementService,
        demo_service: DemoDataService,
        page: SettingsPage,
        import_file_chooser: ImportFileChooser | None = None,
        confirm: ConfirmationDialog | None = None,
        active_demo: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._demo = demo_service
        self._page = page
        self._import_file_chooser = import_file_chooser or self._choose_import_file
        self._confirm = confirm or self._confirm_dialog
        self._active_demo = active_demo
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-data")
        self._futures: set[Future[object]] = set()
        self._busy = False
        self._closing = False
        self._job_completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        page.backup_requested.connect(self.create_backup)
        page.export_requested.connect(self.export_json)
        page.import_requested.connect(self.import_json)
        page.diagnostics_requested.connect(self.create_diagnostics)
        page.demo_create_requested.connect(self.create_demo)
        page.demo_clear_requested.connect(self.clear_demo)

    def prepare_shutdown(self) -> None:
        """Quiesce UI callbacks and cancel queued reads without waiting."""

        self._closing = True
        self._executor.prepare_shutdown()

    def shutdown(self) -> None:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> None:
        self._executor.shutdown()

    @Slot()
    def create_backup(self) -> None:
        self._start(_Operation.BACKUP, self._service.create_sqlite_backup)

    @Slot()
    def export_json(self) -> None:
        self._start(_Operation.EXPORT, self._service.export_json)

    @Slot()
    def import_json(self) -> None:
        if self._busy or self._closing:
            return
        source = self._import_file_chooser()
        if source is None:
            self._page.set_data_operation_status("已取消匯入。")
            return
        self._start(
            _Operation.IMPORT_PREVIEW,
            lambda: self._service.preview_import(source),
        )

    @Slot()
    def create_diagnostics(self) -> None:
        self._start(
            _Operation.DIAGNOSTIC_PREVIEW,
            lambda: self._service.preview_diagnostic_bundle(("version", "health", "logs")),
        )

    @Slot()
    def create_demo(self) -> None:
        self._start(_Operation.DEMO_CREATE, self._demo.ensure_demo_data)

    @Slot()
    def clear_demo(self) -> None:
        if self._busy or self._closing:
            return
        if not self._confirm(
            "清除示範資料",
            (
                "只會刪除標記為 demo 的虛構資料集; 主要資料不會被刪除。"
                + ("目前正在顯示示範資料, 清除後程式會安全關閉。" if self._active_demo else "")
                + "確定繼續?"
            ),
        ):
            self._page.set_data_operation_status("已取消清除示範資料。")
            return
        self._start(_Operation.DEMO_CLEAR, self._demo.clear_demo_data)

    def _start(self, operation: _Operation, work: Callable[[], object]) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self._page.set_data_actions_enabled(False)
        self._page.set_data_operation_status("正在執行本機資料作業…")
        self._launch(operation, work)

    def _launch(self, operation: _Operation, work: Callable[[], object]) -> None:
        def run() -> object:
            try:
                return _Result(operation, payload=work())
            except Exception as exc:  # Local UI boundary; never include file contents.
                return _Result(operation, error=f"{type(exc).__name__}: {exc}")

        future = cast(
            Future[object],
            self._executor.submit(
                run,
                read_only=operation in {_Operation.IMPORT_PREVIEW, _Operation.DIAGNOSTIC_PREVIEW},
            ),
        )
        self._futures.add(future)

        def finished(item: Future[object]) -> None:
            self._futures.discard(item)
            if not self._closing:
                self._job_completed.emit(item.result())

        future.add_done_callback(finished)

    @Slot(object)
    def _handle_result(self, value: object) -> None:
        if self._closing or not isinstance(value, _Result):
            return
        if value.error is not None:
            self._finish(f"本機資料作業失敗: {value.error}", error=True)
            return

        if value.operation is _Operation.IMPORT_PREVIEW:
            preview = value.payload
            if not isinstance(preview, ImportPreview):
                self._finish("匯入預覽格式不正確。", error=True)
                return
            if not self._confirm("確認還原資料", _import_preview_text(preview)):
                self._finish("已取消匯入; 現有資料未變更。")
                return
            source_path = preview.source_path
            document_sha256 = preview.document_sha256
            self._page.set_data_operation_status("正在建立待重啟還原檔…")
            self._launch(
                _Operation.IMPORT_STAGE,
                lambda: self._service.stage_import(
                    source_path,
                    expected_document_sha256=document_sha256,
                    replace_existing_stage=True,
                ),
            )
            return

        if value.operation is _Operation.DIAGNOSTIC_PREVIEW:
            preview = value.payload
            if not isinstance(preview, DiagnosticPreview):
                self._finish("診斷包預覽格式不正確。", error=True)
                return
            if not self._confirm("確認診斷包內容", _diagnostic_preview_text(preview)):
                self._finish("已取消建立診斷包。")
                return
            self._page.set_data_operation_status("正在建立已確認的診斷包…")
            self._launch(
                _Operation.DIAGNOSTIC_CREATE,
                lambda: self._service.create_diagnostic_bundle(
                    preview,
                    confirmed_categories=preview.categories,
                ),
            )
            return

        message = _success_message(value.operation, value.payload)
        self._finish(message)
        if value.operation is _Operation.DEMO_CLEAR and self._active_demo:
            self.restart_required.emit("示範資料已清除; 請重新啟動程式以使用主要資料。")

    def _finish(self, message: str, *, error: bool = False) -> None:
        self._busy = False
        self._page.set_data_actions_enabled(True)
        self._page.set_data_operation_status(message, error=error)

    def _choose_import_file(self) -> Path | None:
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self._page,
            "選擇 Wuwa JSON 備份",
            "",
            "Wuwa JSON 備份 (*.json)",
        )
        return Path(filename) if filename else None

    def _confirm_dialog(self, title: str, message: str) -> bool:
        result = QMessageBox.question(
            self._page,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes


def _import_preview_text(preview: ImportPreview) -> str:
    manifest = preview.manifest
    counts = ", ".join(f"{name}={count}" for name, count in manifest.table_counts)
    return (
        f"來源: {preview.source_path}\n"
        f"備份版本: {manifest.app_version}\n"
        f"Schema: {manifest.schema_revision}\n"
        f"建立時間: {manifest.created_at}\n"
        f"資料庫大小: {manifest.database_size_bytes} bytes\n"
        f"資料列: {counts}\n"
        f"文件 SHA-256: {preview.document_sha256}\n\n"
        "確認後只會建立待還原檔, 不會熱替換目前資料庫。"
        "程式重啟時會先建立 rollback 備份再套用; 已有待還原檔會被取代。"
    )


def _diagnostic_preview_text(preview: DiagnosticPreview) -> str:
    entries = (
        "\n".join(
            f"- [{item.category}] {item.archive_name} ({item.size_bytes} bytes, {item.sha256})"
            for item in preview.entries
        )
        or "- 沒有符合 allowlist 的檔案"
    )
    excluded = ", ".join(preview.excluded_by_design)
    return (
        "將包含:\n"
        f"{entries}\n\n"
        f"明確排除: {excluded}\n\n"
        "診斷包不含資料庫、完整 frame、截圖或 debug ROI。確定建立?"
    )


def _success_message(operation: _Operation, payload: object | None) -> str:
    if operation is _Operation.BACKUP and isinstance(payload, SqliteSnapshotInfo):
        return f"SQLite 備份完成: {payload.path}"
    if operation is _Operation.EXPORT and isinstance(payload, ExportReceipt):
        return f"JSON 備份匯出完成: {payload.path}"
    if operation is _Operation.IMPORT_STAGE and isinstance(payload, StagedRestore):
        return f"還原資料已驗證並暫存: {payload.staged_path}; 請關閉並重新啟動程式。"
    if operation is _Operation.DIAGNOSTIC_CREATE and isinstance(payload, DiagnosticBundleReceipt):
        return f"診斷包完成: {payload.path}"
    if operation is _Operation.DEMO_CREATE and isinstance(payload, DemoDataSnapshot):
        return (
            f"示範資料已建立: {payload.character_count} 名角色, {payload.team_count} 支隊伍; "
            "重新啟動後可依首次設定選取的資料集顯示。"
        )
    if operation is _Operation.DEMO_CLEAR and isinstance(payload, int):
        return f"已清除 {payload} 個示範資料集; 主要資料未變更。"
    return "本機資料作業已完成。"


__all__ = ["ConfirmationDialog", "DataManagementPresenter", "ImportFileChooser"]
