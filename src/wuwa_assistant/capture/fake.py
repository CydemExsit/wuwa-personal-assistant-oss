"""Deterministic in-memory passive capture backend for tests and development."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from .backend import BgrImage, CapturedFrame, FrameLeaseAudit, MonitorInfo
from .errors import CaptureError, CaptureExhaustedError, InvalidMonitorError


class FakeCaptureBackend:
    """Return defensive copies of an explicitly supplied finite frame sequence."""

    def __init__(
        self,
        frames: Iterable[BgrImage],
        *,
        monitors: Iterable[MonitorInfo] | None = None,
        audit: FrameLeaseAudit | None = None,
    ) -> None:
        self._frames = tuple(np.ascontiguousarray(frame).copy() for frame in frames)
        self._monitors = tuple(monitors or (MonitorInfo(1, 0, 0, 2560, 1440, True),))
        if not self._monitors:
            raise ValueError("at least one fake physical monitor is required")
        if len({monitor.index for monitor in self._monitors}) != len(self._monitors):
            raise ValueError("fake monitor indices must be unique")
        self._audit = audit or FrameLeaseAudit()
        self._next_frame = 0
        self._sequence = 0
        self._closed = False

    @property
    def audit(self) -> FrameLeaseAudit:
        return self._audit

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        self._require_open()
        return self._monitors

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        self._require_open()
        self._require_monitor(monitor_index)
        if self._next_frame >= len(self._frames):
            raise CaptureExhaustedError("the finite fake capture sequence is exhausted")

        pixels = self._frames[self._next_frame].copy()
        next_sequence = self._sequence + 1
        frame = CapturedFrame.now(
            pixels,
            monitor_index,
            sequence=next_sequence,
            audit=self._audit,
        )
        self._next_frame += 1
        self._sequence = next_sequence
        return frame

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise CaptureError("the fake capture backend is closed")

    def _require_monitor(self, monitor_index: int) -> None:
        if not any(monitor.index == monitor_index for monitor in self._monitors):
            raise InvalidMonitorError(f"monitor {monitor_index} is not available")


__all__ = ["FakeCaptureBackend"]
