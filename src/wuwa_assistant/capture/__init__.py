"""Replaceable, passive whole-monitor capture with an ROI-only live boundary."""

from .backend import (
    BgrImage,
    CaptureBackend,
    CaptureConfig,
    CapturedFrame,
    CaptureStatus,
    FrameLeaseAudit,
    FrameLeaseAuditSnapshot,
    MonitorInfo,
)
from .errors import (
    CaptureError,
    CaptureExhaustedError,
    CaptureThreadError,
    CaptureUnavailableError,
    FrameReleasedError,
    InvalidMonitorError,
)
from .fake import FakeCaptureBackend
from .image_sequence import ImageSequenceBackend, load_sequence_image
from .mss_backend import MssMonitorCaptureBackend
from .roi_packet import (
    ALLOWED_ROIS,
    CONTENT_ROIS_BY_PAGE,
    PROBE_ROIS,
    ExtractedRois,
    RoiExtractor,
    RoiPacket,
    RoiPacketKind,
)

__all__ = [
    "ALLOWED_ROIS",
    "CONTENT_ROIS_BY_PAGE",
    "PROBE_ROIS",
    "BgrImage",
    "CaptureBackend",
    "CaptureConfig",
    "CaptureError",
    "CaptureExhaustedError",
    "CaptureStatus",
    "CaptureThreadError",
    "CaptureUnavailableError",
    "CapturedFrame",
    "ExtractedRois",
    "FakeCaptureBackend",
    "FrameLeaseAudit",
    "FrameLeaseAuditSnapshot",
    "FrameReleasedError",
    "ImageSequenceBackend",
    "InvalidMonitorError",
    "MonitorInfo",
    "MssMonitorCaptureBackend",
    "RoiExtractor",
    "RoiPacket",
    "RoiPacketKind",
    "load_sequence_image",
]
