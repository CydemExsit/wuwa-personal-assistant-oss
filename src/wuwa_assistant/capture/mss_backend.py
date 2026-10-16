"""Passive whole-monitor capture through a lazy, replaceable MSS adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from threading import get_ident
from typing import Protocol, cast

import numpy as np

from wuwa_assistant.display import RESOLUTIONS

from .backend import BgrImage, CapturedFrame, FrameLeaseAudit, MonitorInfo
from .errors import (
    CaptureError,
    CaptureThreadError,
    CaptureUnavailableError,
    InvalidMonitorError,
)

type _RawMonitor = Mapping[str, object]


class _MssContext(Protocol):
    @property
    def monitors(self) -> Sequence[_RawMonitor]: ...

    def grab(self, monitor: Mapping[str, int]) -> object: ...

    def close(self) -> None: ...


type MssFactory = Callable[[], _MssContext]


def _default_mss_factory() -> _MssContext:
    try:
        import mss
    except ImportError as exc:
        raise CaptureUnavailableError(
            "The local MSS capture backend is not installed; capture remains disabled"
        ) from exc
    return cast(_MssContext, mss.MSS())


class MssMonitorCaptureBackend:
    """Capture only a user-selected physical monitor using ``grab`` in one thread."""

    def __init__(
        self,
        *,
        mss_factory: MssFactory | None = None,
        audit: FrameLeaseAudit | None = None,
    ) -> None:
        self._factory = mss_factory or _default_mss_factory
        self._context: _MssContext | None = None
        self._owner_thread_id: int | None = None
        self._audit = audit or FrameLeaseAudit()
        self._sequence = 0
        self._closed = False

    @property
    def audit(self) -> FrameLeaseAudit:
        return self._audit

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        context = self._get_context()
        # MSS index 0 represents the combined virtual desktop.  It is deliberately
        # not represented as a selectable MonitorInfo.
        return tuple(
            self._monitor_info(index, raw)
            for index, raw in enumerate(context.monitors[1:], start=1)
        )

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        self._require_physical_index(monitor_index)
        context = self._get_context()
        monitors = context.monitors
        if monitor_index >= len(monitors):
            raise InvalidMonitorError(f"monitor {monitor_index} is not available")

        raw_monitor = monitors[monitor_index]
        monitor = self._monitor_info(monitor_index, raw_monitor)
        if not monitor.is_supported_size:
            raise InvalidMonitorError(
                f"monitor {monitor_index} must be 1920x1080 or 2560x1440; "
                f"got {monitor.width}x{monitor.height}"
            )

        try:
            # Only the four integer bounds are needed for capture.  In particular,
            # hardware identifiers exposed by a platform backend are never retained
            # or forwarded to the grab call.
            screenshot = context.grab(monitor.bounds)
        except Exception as exc:
            raise CaptureError(f"passive monitor capture failed: {exc}") from exc

        pixels = self._bgr_copy(screenshot)
        if pixels.shape[:2] != (monitor.height, monitor.width):
            raise CaptureUnavailableError("擷取畫面尺寸與所選顯示器不符, 請重新選擇顯示器。")
        self._sequence += 1
        return CapturedFrame.now(
            pixels,
            monitor_index,
            sequence=self._sequence,
            audit=self._audit,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._context is not None:
            self._assert_owner_thread()
            self._context.close()
            self._context = None
        self._closed = True

    def _get_context(self) -> _MssContext:
        if self._closed:
            raise CaptureError("the MSS capture backend is closed")
        self._claim_or_assert_owner_thread()
        if self._context is None:
            try:
                self._context = self._factory()
            except CaptureError:
                raise
            except Exception as exc:
                raise CaptureUnavailableError(f"could not initialize local MSS: {exc}") from exc
        return self._context

    def _claim_or_assert_owner_thread(self) -> None:
        current = get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
        elif current != self._owner_thread_id:
            raise CaptureThreadError(
                "MSS must be listed, captured, and closed in the worker thread that created it"
            )

    def _assert_owner_thread(self) -> None:
        if self._owner_thread_id is not None and get_ident() != self._owner_thread_id:
            raise CaptureThreadError(
                "MSS must be listed, captured, and closed in the worker thread that created it"
            )

    @staticmethod
    def _require_physical_index(monitor_index: int) -> None:
        if isinstance(monitor_index, bool) or monitor_index < 1:
            raise InvalidMonitorError(
                "monitor index 0 is the combined virtual desktop and cannot be captured"
            )

    @staticmethod
    def _monitor_info(index: int, raw: _RawMonitor) -> MonitorInfo:
        try:
            left = _required_monitor_int(raw, "left")
            top = _required_monitor_int(raw, "top")
            width = _required_monitor_int(raw, "width")
            height = _required_monitor_int(raw, "height")
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureUnavailableError("MSS returned malformed monitor geometry") from exc

        raw_primary = raw.get("is_primary")
        is_primary = raw_primary if isinstance(raw_primary, bool) else left == 0 and top == 0
        raw_name = raw.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        return MonitorInfo(
            index=index,
            left=left,
            top=top,
            width=width,
            height=height,
            is_primary=is_primary,
            name=name,
        )

    @staticmethod
    def _bgr_copy(screenshot: object) -> BgrImage:
        try:
            raw = np.asarray(screenshot, dtype=np.uint8)
        except (TypeError, ValueError) as exc:
            raise CaptureUnavailableError("MSS returned invalid pixel data") from exc

        if raw.shape in {(h, w, 4) for w, h in RESOLUTIONS.values()}:
            # MSS exposes BGRA.  Dropping alpha preserves the required BGR order.
            return cast(BgrImage, np.ascontiguousarray(raw[:, :, :3]))
        if raw.shape in {(h, w, 3) for w, h in RESOLUTIONS.values()}:
            return np.ascontiguousarray(raw).copy()
        raise CaptureUnavailableError(
            f"MSS returned an unexpected frame shape {raw.shape!r}; "
            "expected 1080p/1440p BGRA or BGR"
        )


def _required_monitor_int(raw: _RawMonitor, key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"monitor {key} is not an integer")
    return int(value)


__all__ = ["MssFactory", "MssMonitorCaptureBackend"]
