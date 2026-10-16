"""Offline RapidOCR adapter using only bundled, local PP-OCRv6 model files."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import ceil, floor, isfinite
from os import cpu_count
from pathlib import Path
from threading import Lock
from typing import ClassVar, Protocol, cast

import numpy as np

from wuwa_assistant.models import OcrToken

from .base import OcrBackendError, OcrImage, _validate_image
from .cache import CacheInfo, ExactOcrCache


class _RapidBackend(Protocol):
    def __call__(
        self,
        image: OcrImage,
        *,
        use_det: bool,
        use_cls: bool,
        use_rec: bool,
        text_score: float,
    ) -> object: ...


type _EngineFactory = Callable[[], _RapidBackend]


class _RapidDetector(Protocol):
    limit_type: str


class _ConfiguredRapidBackend(_RapidBackend, Protocol):
    text_det: _RapidDetector


class _RoiSizedBackend:
    """Avoid document-sized detector upscaling for small, native-resolution UI crops.

    RapidOCR 3.9.2 defaults to a 736-pixel *minimum* detector side, turning a
    55x170 toast into roughly a 736x2275 detector input. Small UI labels already have
    legible native glyphs; its ``max`` mode preserves their native resolution.
    Larger stat panels retain the calibrated default detection geometry.  This
    adapter owns one model set and serializes the temporary detector setting.
    """

    def __init__(self, engine: _ConfiguredRapidBackend) -> None:
        self._engine = engine
        self._lock = Lock()

    def __call__(
        self,
        image: OcrImage,
        *,
        use_det: bool,
        use_cls: bool,
        use_rec: bool,
        text_score: float,
    ) -> object:
        with self._lock:
            detector = self._engine.text_det
            previous_limit = detector.limit_type
            # Restrict the optimization to bounded one/two-line UI anchors.
            # Keep detector behavior unchanged for larger panels and long strips.
            if use_det and min(image.shape[:2]) <= 128 and max(image.shape[:2]) <= 680:
                detector.limit_type = "max"
            try:
                return self._engine(
                    image,
                    use_det=use_det,
                    use_cls=use_cls,
                    use_rec=use_rec,
                    text_score=text_score,
                )
            finally:
                detector.limit_type = previous_limit


class RapidOcrEngine:
    """Lazily initialized RapidOCR 3.9 adapter.

    The default factory resolves the three PP-OCRv6/PP-OCRv4 model files bundled
    inside the installed ``rapidocr`` wheel and supplies their explicit local paths.
    Missing files therefore fail closed instead of triggering RapidOCR's downloader.
    A factory hook exists so tests never need to initialize the native runtime.
    """

    _MODEL_FILENAMES: ClassVar[dict[str, str]] = {
        "Det.model_path": "PP-OCRv6_det_small.onnx",
        "Cls.model_path": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        "Rec.model_path": "PP-OCRv6_rec_small.onnx",
    }

    def __init__(
        self, *, engine_factory: _EngineFactory | None = None, cache_entries: int = 64
    ) -> None:
        self._engine_factory = engine_factory
        self._engine: _RapidBackend | None = None
        self._initialization_lock = Lock()
        self._cache = ExactOcrCache(self._recognize, max_entries=cache_entries)

    @property
    def is_initialized(self) -> bool:
        return self._engine is not None

    def recognize(self, image: OcrImage, *, detect: bool = True) -> list[OcrToken]:
        return self._cache.recognize(image, detect=detect)

    def clear_cache(self) -> None:
        self._cache.clear()

    @property
    def cache_info(self) -> CacheInfo:
        return self._cache.info()

    def _recognize(self, image: OcrImage, *, detect: bool = True) -> list[OcrToken]:
        """Recognize one ROI and retain uncalibrated backend line confidences.

        ``detect=False`` is intended for a tightly cropped single text line.  Because
        recognition-only output has no detector polygon, its bounding box is the
        complete ROI.  For detector output, quadrilaterals are converted to the
        axis-aligned, ROI-relative box required by :class:`OcrToken`.
        """

        validated = _validate_image(image)
        contiguous = cast(OcrImage, np.ascontiguousarray(validated))
        output = self._get_engine()(
            contiguous,
            use_det=detect,
            use_cls=False,
            use_rec=True,
            # Keep low-confidence raw candidates. Semantic acceptance belongs to parsers.
            text_score=0.0,
        )
        return self._tokens_from_output(output, contiguous.shape, detect=detect)

    def _get_engine(self) -> _RapidBackend:
        engine = self._engine
        if engine is not None:
            return engine

        with self._initialization_lock:
            if self._engine is None:
                factory = self._engine_factory or self._create_local_engine
                self._engine = factory()
            return self._engine

    @classmethod
    def _create_local_engine(cls) -> _RapidBackend:
        # Import only on the first OCR request so tests and non-OCR CLI paths stay light.
        import rapidocr
        from rapidocr import RapidOCR

        package_file = getattr(rapidocr, "__file__", None)
        if not package_file:
            raise OcrBackendError("Cannot locate the installed rapidocr package")

        model_directory = Path(package_file).resolve().parent / "models"
        params: dict[str, object] = {
            "Global.log_level": "warning",
            # Default ONNX pools can occupy every core for each model. Bound
            # inference parallelism so passive recognition leaves CPU for the game.
            "EngineConfig.onnxruntime.intra_op_num_threads": min(4, cpu_count() or 1),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.use_cann": False,
        }
        missing: list[Path] = []
        for parameter, filename in cls._MODEL_FILENAMES.items():
            model_path = model_directory / filename
            if not model_path.is_file():
                missing.append(model_path)
            params[parameter] = str(model_path)

        if missing:
            rendered = ", ".join(str(path) for path in missing)
            raise OcrBackendError(
                "Bundled RapidOCR models are missing; refusing network download: " + rendered
            )

        return _RoiSizedBackend(cast(_ConfiguredRapidBackend, RapidOCR(params=params)))

    @classmethod
    def _tokens_from_output(
        cls,
        output: object,
        image_shape: tuple[int, ...],
        *,
        detect: bool,
    ) -> list[OcrToken]:
        if output is None:
            return []

        texts = cast(Sequence[object] | None, getattr(output, "txts", None))
        scores = cast(Sequence[object] | None, getattr(output, "scores", None))
        if texts is None and scores is None:
            return []
        if texts is None or scores is None or len(texts) != len(scores):
            raise OcrBackendError("RapidOCR returned mismatched texts and confidence scores")

        height, width = image_shape[:2]
        if detect:
            boxes = cast(Sequence[object] | None, getattr(output, "boxes", None))
            if boxes is None or len(boxes) != len(texts):
                raise OcrBackendError("RapidOCR detector returned mismatched text boxes")
            bboxes = [cls._axis_aligned_bbox(box, width=width, height=height) for box in boxes]
        else:
            bboxes = [(0, 0, width, height)] * len(texts)

        tokens: list[OcrToken] = []
        for text, raw_score, bbox in zip(texts, scores, bboxes, strict=True):
            if not isinstance(text, str):
                raise OcrBackendError("RapidOCR returned a non-string recognition result")
            if isinstance(raw_score, bool) or not isinstance(
                raw_score, (int, float, np.integer, np.floating)
            ):
                raise OcrBackendError("RapidOCR returned a non-numeric confidence")
            confidence = float(raw_score)
            if not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise OcrBackendError(
                    f"RapidOCR confidence must be within [0, 1], got {confidence!r}"
                )
            tokens.append(OcrToken(text=text, confidence=confidence, bbox=bbox))
        return tokens

    @staticmethod
    def _axis_aligned_bbox(
        raw_box: object,
        *,
        width: int,
        height: int,
    ) -> tuple[int, int, int, int]:
        try:
            points = np.asarray(raw_box, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise OcrBackendError("RapidOCR returned a non-numeric text box") from exc
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise OcrBackendError(
                f"RapidOCR text box must contain four finite xy points, got {points.shape}"
            )

        x1 = max(0, min(width, floor(float(points[:, 0].min()))))
        y1 = max(0, min(height, floor(float(points[:, 1].min()))))
        x2 = max(0, min(width, ceil(float(points[:, 0].max()))))
        y2 = max(0, min(height, ceil(float(points[:, 1].max()))))
        return (x1, y1, x2, y2)
