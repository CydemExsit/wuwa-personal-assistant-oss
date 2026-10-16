"""Read-only image-sequence backend for simulating passive capture locally."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

import cv2
import numpy as np

from .backend import BgrImage, CapturedFrame, FrameLeaseAudit, MonitorInfo
from .errors import CaptureError, CaptureExhaustedError, InvalidMonitorError

type ImageLoader = Callable[[Path], BgrImage]


def load_sequence_image(path: Path) -> BgrImage:
    """Decode one local image as BGR without writing or renaming the source file."""

    if not path.is_file():
        raise FileNotFoundError(path)
    encoded = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"unsupported or corrupt image: {path}")
    return cast(BgrImage, image)


class ImageSequenceBackend:
    """Yield ordered user-provided static images once, with no recording API."""

    def __init__(
        self,
        paths: Iterable[str | Path],
        *,
        monitor_index: int = 1,
        image_loader: ImageLoader = load_sequence_image,
        audit: FrameLeaseAudit | None = None,
    ) -> None:
        if isinstance(monitor_index, bool) or monitor_index < 1:
            raise ValueError("monitor_index must identify a physical monitor")
        self._paths = tuple(Path(path).expanduser().resolve() for path in paths)
        if not self._paths:
            raise ValueError("an image sequence needs at least one local path")
        self._monitor = MonitorInfo(monitor_index, 0, 0, 2560, 1440, True, "image-sequence")
        self._image_loader = image_loader
        self._audit = audit or FrameLeaseAudit()
        self._next_path = 0
        self._sequence = 0
        self._closed = False

    @property
    def audit(self) -> FrameLeaseAudit:
        return self._audit

    def list_monitors(self) -> tuple[MonitorInfo, ...]:
        self._require_open()
        return (self._monitor,)

    def capture_monitor(self, monitor_index: int) -> CapturedFrame:
        self._require_open()
        if monitor_index != self._monitor.index:
            raise InvalidMonitorError(f"monitor {monitor_index} is not available")
        if self._next_path >= len(self._paths):
            raise CaptureExhaustedError("the finite image sequence is exhausted")

        path = self._paths[self._next_path]
        pixels = np.ascontiguousarray(self._image_loader(path)).copy()
        next_sequence = self._sequence + 1
        frame = CapturedFrame.now(
            pixels,
            monitor_index,
            sequence=next_sequence,
            source_file=str(path),
            audit=self._audit,
        )
        self._next_path += 1
        self._sequence = next_sequence
        return frame

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise CaptureError("the image-sequence backend is closed")


__all__ = ["ImageLoader", "ImageSequenceBackend", "load_sequence_image"]
