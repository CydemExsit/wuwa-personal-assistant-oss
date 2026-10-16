"""Pure-Python coordination for safe, passive live ROI recognition."""

from wuwa_assistant.capture.roi_packet import RoiPacket, RoiPacketKind

from .analyzer import LiveEchoRoiAnalyzer
from .coordinator import LiveRecognitionCoordinator, RoiAnalyzer
from .dto import (
    AnalysisResult,
    CoordinatorStats,
    GenerationToken,
    LiveRecognitionResult,
    LiveRuntimeError,
    LiveStatus,
    LiveStatusUpdate,
    PacketDisposition,
)
from .fakes import FakeRoiAnalyzer
from .runtime import LiveCaptureRuntime

__all__ = [
    "AnalysisResult",
    "CoordinatorStats",
    "FakeRoiAnalyzer",
    "GenerationToken",
    "LiveCaptureRuntime",
    "LiveEchoRoiAnalyzer",
    "LiveRecognitionCoordinator",
    "LiveRecognitionResult",
    "LiveRuntimeError",
    "LiveStatus",
    "LiveStatusUpdate",
    "PacketDisposition",
    "RoiAnalyzer",
    "RoiPacket",
    "RoiPacketKind",
]
