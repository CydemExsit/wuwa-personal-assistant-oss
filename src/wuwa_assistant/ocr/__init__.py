"""Offline OCR contracts and backend adapters."""

from .base import FakeOcrEngine, OcrBackendError, OcrEngine, OcrImage
from .rapidocr_engine import RapidOcrEngine

__all__ = [
    "FakeOcrEngine",
    "OcrBackendError",
    "OcrEngine",
    "OcrImage",
    "RapidOcrEngine",
]
