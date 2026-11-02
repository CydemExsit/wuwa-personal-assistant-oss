"""One asynchronous editor and write queue shared by all three entry points."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QWidget

from wuwa_assistant.application.tacet_catalog import TacetCatalog, load_tacet_catalog
from wuwa_assistant.application.tacet_stock import TacetStockEditRequest, TacetStockService
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.dialogs.tacet_stock import TacetStockDialog


@dataclass(frozen=True)
class _Result:
    catalog: TacetCatalog | None = None
    stocked: frozenset[str] = frozenset()
    saved: bool = False
    error: str = ""


class TacetStockPresenter(QObject):
    changed = Signal()
    busy_changed = Signal(bool)
    failed = Signal(str)
    _completed = Signal(object)

    def __init__(
        self,
        service: TacetStockService,
        data_set_id: str,
        *,
        cache_directory: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._data_set_id = data_set_id
        self._cache_directory = cache_directory
        self._owner = parent
        self.dialog: TacetStockDialog | None = None
        self._busy = False
        self._closing = False
        self._needs_reload = False
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-tacet-stock")
        self._completed.connect(self._apply, Qt.ConnectionType.QueuedConnection)

    def open(self) -> None:
        if self._closing:
            return
        if self.dialog is None:
            self.dialog = TacetStockDialog(self._owner)
            self.dialog.edit_requested.connect(self.save)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.set_busy(self._busy)
        if not self._busy:
            self._submit(None)

    def save(self, payload: object) -> None:
        if not self._closing and not self._busy and isinstance(payload, TacetStockEditRequest):
            if self._needs_reload:
                self._submit(None)
            else:
                self._submit(payload)

    def _submit(self, request: TacetStockEditRequest | None) -> None:
        self._busy = True
        self.busy_changed.emit(True)
        if self.dialog is not None:
            self.dialog.set_busy(True)
            self.dialog.status.setText("正在保存…" if request else "正在載入…")

        def work() -> _Result:
            saved = False
            try:
                catalog = load_tacet_catalog(self._cache_directory)
                if request is not None:
                    self.service.save(
                        self._data_set_id, catalog, request.field_ids, has_stock=request.has_stock
                    )
                    saved = True
                return _Result(
                    catalog, self.service.stocked_field_ids(self._data_set_id, catalog), saved
                )
            except Exception as exc:
                return _Result(saved=saved, error=f"無音區庫存更新失敗: {exc}")

        future = self._executor.submit(work, read_only=request is None)
        future.add_done_callback(self._done)

    def _done(self, future: Future[_Result]) -> None:
        if not self._closing and not future.cancelled():
            self._completed.emit(future.result())

    def _apply(self, result: _Result) -> None:
        if self._closing:
            return
        self._busy = False
        # A committed write followed by failed readback must not leave clickable
        # controls showing old values. Reopen/reload before accepting another edit.
        self._needs_reload = result.catalog is None and (result.saved or self._needs_reload)
        self.busy_changed.emit(False)
        if self.dialog is not None:
            if result.catalog is not None:
                self.dialog.set_stock(result.catalog, result.stocked)
            self.dialog.set_busy(self._needs_reload)
            self.dialog.status.setText(
                result.error
                or ("庫存狀態已保存。" if result.saved else "點擊狀態即可切換並立即保存。")
            )
        if result.error:
            self.failed.emit(result.error)
        if result.saved:
            # A committed write still notifies readers even if its subsequent
            # reload failed. Failed transactions never emit a success event.
            self.changed.emit()

    def prepare_shutdown(self) -> None:
        self._closing = True
        if self.dialog is not None:
            self.dialog.close()
        self._executor.prepare_shutdown()

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        # Called by the window's drain worker, after GUI preparation.
        self._closing = True
        self._executor.shutdown()
        return True
