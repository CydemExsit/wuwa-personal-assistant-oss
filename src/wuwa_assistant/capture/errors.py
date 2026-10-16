"""Friendly errors for passive monitor capture."""

from __future__ import annotations


class CaptureError(RuntimeError):
    """Base class for failures that do not justify interacting with a game process."""


class CaptureUnavailableError(CaptureError):
    """Raised when the local monitor capture backend is unavailable."""


class InvalidMonitorError(CaptureError):
    """Raised when a monitor is absent, virtual, or not 2560x1440."""


class CaptureExhaustedError(CaptureError):
    """Raised when a finite fake or image sequence has no next frame."""


class CaptureThreadError(CaptureError):
    """Raised when a thread-affine backend is called from another thread."""


class FrameReleasedError(CaptureError):
    """Raised when full-frame pixels are requested after ROI extraction."""


__all__ = [
    "CaptureError",
    "CaptureExhaustedError",
    "CaptureThreadError",
    "CaptureUnavailableError",
    "FrameReleasedError",
    "InvalidMonitorError",
]
