"""Passive, monitor-only capture contracts and full-frame lease handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from time import monotonic_ns
from typing import Protocol, Self, cast

import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.display import DEFAULT_RESOLUTION, RESOLUTIONS
from wuwa_assistant.vision.rois import REFERENCE_HEIGHT, REFERENCE_WIDTH, Roi

from .errors import FrameReleasedError

type BgrImage = NDArray[np.uint8]


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """One physical monitor exposed by a passive capture backend."""

    index: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or self.index < 1:
            raise ValueError("monitor index 0 is the virtual desktop and is not selectable")
        if self.width < 1 or self.height < 1:
            raise ValueError("monitor dimensions must be positive")

    @property
    def stable_key(self) -> str:
        """Return a geometry key suitable for detecting monitor-layout changes."""

        return f"{self.left}:{self.top}:{self.width}x{self.height}"

    @property
    def is_reference_size(self) -> bool:
        return (self.width, self.height) == (REFERENCE_WIDTH, REFERENCE_HEIGHT)

    @property
    def is_supported_size(self) -> bool:
        return (self.width, self.height) in RESOLUTIONS.values()

    @property
    def bounds(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """User-controlled live capture settings with conservative bounds."""

    monitor_index: int
    sample_hz: float = 5.0
    resolution: str = DEFAULT_RESOLUTION

    def __post_init__(self) -> None:
        if isinstance(self.monitor_index, bool) or self.monitor_index < 1:
            raise ValueError("monitor_index must identify a physical monitor (1 or greater)")
        if isinstance(self.sample_hz, bool) or not 2.0 <= self.sample_hz <= 8.0:
            raise ValueError("sample_hz must be between 2 and 8")
        if self.resolution not in RESOLUTIONS:
            raise ValueError("resolution must be 1920x1080 or 2560x1440")

    @property
    def interval_seconds(self) -> float:
        return 1.0 / self.sample_hz


class CaptureStatus(StrEnum):
    """Structured states presented by the independent desktop window."""

    NOT_ENABLED = "not_enabled"
    STARTING = "starting"
    WAITING_FOR_ECHO_PAGE = "waiting_for_echo_page"
    WAITING_FOR_REQUEST = "waiting_for_request"
    TRANSITIONING = "transitioning"
    RECOGNIZING = "recognizing"
    STABLE = "stable"
    LOW_CONFIDENCE = "low_confidence"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FrameLeaseAuditSnapshot:
    acquired: int
    released: int
    active: int
    max_active: int


class FrameLeaseAudit:
    """Metadata-only instrumentation proving that full-frame leases are released."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_ids: set[int] = set()
        self._acquired = 0
        self._released = 0
        self._max_active = 0

    def acquired(self, lease_id: int) -> None:
        with self._lock:
            if lease_id in self._active_ids:
                raise RuntimeError("duplicate frame lease id")
            self._active_ids.add(lease_id)
            self._acquired += 1
            self._max_active = max(self._max_active, len(self._active_ids))

    def released(self, lease_id: int) -> None:
        with self._lock:
            if lease_id not in self._active_ids:
                return
            self._active_ids.remove(lease_id)
            self._released += 1

    @property
    def snapshot(self) -> FrameLeaseAuditSnapshot:
        with self._lock:
            return FrameLeaseAuditSnapshot(
                acquired=self._acquired,
                released=self._released,
                active=len(self._active_ids),
                max_active=self._max_active,
            )

    def assert_all_released(self) -> None:
        snapshot = self.snapshot
        if snapshot.active:
            raise AssertionError(f"{snapshot.active} full-frame lease(s) remain active")


class CapturedFrame:
    """Short-lived in-memory BGR frame that must be released after ROI copying.

    The constructor preserves the phase-one ``pixels, captured_at, monitor_index``
    contract.  Pixel data is intentionally omitted from ``repr`` and becomes
    inaccessible immediately after :meth:`release`.
    """

    __slots__ = (
        "_audit",
        "_lease_id",
        "_pixels",
        "captured_at",
        "monitor_index",
        "monotonic_timestamp_ns",
        "sequence",
        "source_file",
    )

    def __init__(
        self,
        pixels: BgrImage,
        captured_at: str,
        monitor_index: int,
        *,
        monotonic_timestamp_ns: int | None = None,
        sequence: int = 0,
        source_file: str | None = None,
        audit: FrameLeaseAudit | None = None,
    ) -> None:
        _validate_reference_bgr_frame(pixels)
        if isinstance(monitor_index, bool) or monitor_index < 1:
            raise ValueError("monitor_index must identify a physical monitor")
        if sequence < 0:
            raise ValueError("sequence may not be negative")

        self._pixels: BgrImage | None = pixels
        self.captured_at = captured_at
        self.monitor_index = monitor_index
        self.monotonic_timestamp_ns = (
            monotonic_ns() if monotonic_timestamp_ns is None else monotonic_timestamp_ns
        )
        self.sequence = sequence
        self.source_file = source_file
        self._audit = audit
        self._lease_id = id(self)
        if audit is not None:
            audit.acquired(self._lease_id)

    @classmethod
    def now(
        cls,
        pixels: BgrImage,
        monitor_index: int,
        *,
        sequence: int = 0,
        source_file: str | None = None,
        audit: FrameLeaseAudit | None = None,
    ) -> CapturedFrame:
        return cls(
            pixels,
            datetime.now(UTC).isoformat(),
            monitor_index,
            monotonic_timestamp_ns=monotonic_ns(),
            sequence=sequence,
            source_file=source_file,
            audit=audit,
        )

    @property
    def pixels(self) -> BgrImage:
        pixels = self._pixels
        if pixels is None:
            raise FrameReleasedError("the full monitor frame has already been released")
        return pixels

    @property
    def is_released(self) -> bool:
        return self._pixels is None

    def copy_roi(self, roi: Roi) -> BgrImage:
        """Return an independent contiguous ROI without exposing a NumPy view."""

        pixels = self.pixels
        height, width = pixels.shape[:2]
        if (width, height) == (REFERENCE_WIDTH, REFERENCE_HEIGHT):
            return np.ascontiguousarray(roi.crop(pixels, copy=True))
        # Map each bounded crop to the selected native resolution, then normalize
        # only that ROI for existing classifier/parser coordinates. Never resize,
        # queue or persist a second full monitor frame.
        import cv2

        x1 = round(roi.x1 * width / REFERENCE_WIDTH)
        x2 = round(roi.x2 * width / REFERENCE_WIDTH)
        y1 = round(roi.y1 * height / REFERENCE_HEIGHT)
        y2 = round(roi.y2 * height / REFERENCE_HEIGHT)
        return cast(
            BgrImage,
            cv2.resize(
                pixels[y1:y2, x1:x2], (roi.width, roi.height), interpolation=cv2.INTER_CUBIC
            ),
        )

    def release(self) -> None:
        """Drop the only frame reference retained by this lease; idempotent."""

        if self._pixels is None:
            return
        self._pixels = None
        if self._audit is not None:
            self._audit.released(self._lease_id)

    def __enter__(self) -> Self:
        if self.is_released:
            raise FrameReleasedError("cannot enter a released frame lease")
        return self

    def __exit__(self, *_exception: object) -> None:
        self.release()

    def __repr__(self) -> str:
        return (
            "CapturedFrame("
            f"captured_at={self.captured_at!r}, monitor_index={self.monitor_index}, "
            f"sequence={self.sequence}, released={self.is_released})"
        )

    def __del__(self) -> None:
        if getattr(self, "_pixels", None) is not None:
            self.release()


class CaptureBackend(Protocol):
    """Capture a selected whole monitor without any game-process interaction.

    Implementations return an in-memory lease only.  The interface deliberately
    contains no window discovery, process handle, input, screenshot-save, or video API.
    """

    @property
    def audit(self) -> FrameLeaseAudit:
        """Return metadata-only lease instrumentation."""

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        """List selectable physical monitors; virtual desktop index 0 is excluded."""

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        """Return one passive in-memory desktop frame lease."""

    def close(self) -> None:
        """Release backend resources without capturing another frame."""


def _validate_reference_bgr_frame(pixels: object) -> None:
    if not isinstance(pixels, np.ndarray):
        raise TypeError("captured pixels must be a NumPy array")
    if pixels.dtype != np.uint8:
        raise TypeError("captured pixels must use uint8")
    expected = {(height, width, 3) for width, height in RESOLUTIONS.values()}
    if pixels.shape not in expected:
        raise ValueError(
            f"captured monitor must be 1920x1080 or 2560x1440 BGR; got shape {pixels.shape!r}"
        )


__all__ = [
    "BgrImage",
    "CaptureBackend",
    "CaptureConfig",
    "CaptureStatus",
    "CapturedFrame",
    "FrameLeaseAudit",
    "FrameLeaseAuditSnapshot",
    "MonitorInfo",
]
