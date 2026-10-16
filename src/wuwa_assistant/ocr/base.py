"""OCR contracts and deterministic test doubles.

Only in-memory NumPy arrays are accepted by this layer.  File loading, ROI selection,
preprocessing, and persistence belong to the caller so an OCR backend cannot
accidentally upload or retain a full screenshot.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
from typing import Protocol, cast, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.models import OcrToken

type OcrImage = NDArray[np.uint8]


class OcrBackendError(RuntimeError):
    """Raised when an OCR backend returns malformed or internally inconsistent data."""


@runtime_checkable
class OcrEngine(Protocol):
    """Replaceable, offline OCR engine operating on one already-cropped ROI."""

    def recognize(self, image: OcrImage, *, detect: bool = True) -> list[OcrToken]:
        """Return ROI-relative OCR lines without applying semantic corrections."""


def _validate_image(image: object) -> OcrImage:
    """Validate the common image contract without copying or changing pixels."""

    if not isinstance(image, np.ndarray):
        raise TypeError("OCR input must be a numpy.ndarray ROI, never a path or URL")
    if image.dtype != np.uint8:
        raise TypeError(f"OCR input dtype must be uint8, got {image.dtype}")
    if image.size == 0:
        raise ValueError("OCR input ROI must not be empty")
    if image.ndim == 2:
        return cast(OcrImage, image)
    if image.ndim == 3 and image.shape[2] in (1, 3, 4):
        return cast(OcrImage, image)
    raise ValueError("OCR input must be HxW grayscale or HxWx{1,3,4} uint8 pixels")


def _copy_tokens(tokens: Iterable[OcrToken]) -> tuple[OcrToken, ...]:
    return tuple(
        OcrToken(
            text=token.text,
            confidence=token.confidence,
            bbox=(token.bbox[0], token.bbox[1], token.bbox[2], token.bbox[3]),
        )
        for token in tokens
    )


class FakeOcrEngine:
    """Deterministic OCR fake with image-keyed and queued responses.

    Image-keyed responses take precedence and can be reused any number of times.
    Unmatched calls consume queued responses in order; once the queue is exhausted,
    the fake returns an empty list.  Returned tokens are defensive copies so parser
    tests cannot mutate later fake results.
    """

    def __init__(
        self,
        queued_responses: Iterable[Iterable[OcrToken]] = (),
        *,
        tokens_by_key: Mapping[str, Iterable[OcrToken]] | None = None,
    ) -> None:
        self._queued_responses = tuple(_copy_tokens(tokens) for tokens in queued_responses)
        self._tokens_by_key = {
            key: _copy_tokens(tokens) for key, tokens in (tokens_by_key or {}).items()
        }
        self._queue_index = 0
        self._seen_keys: list[str] = []
        self._detect_history: list[bool] = []

    @staticmethod
    def key_for_image(image: OcrImage) -> str:
        """Return a stable content key including shape and dtype."""

        validated = _validate_image(image)
        contiguous = np.ascontiguousarray(validated)
        digest = sha256()
        digest.update(str(contiguous.shape).encode("ascii"))
        digest.update(contiguous.dtype.str.encode("ascii"))
        digest.update(contiguous.tobytes(order="C"))
        return digest.hexdigest()

    @property
    def call_count(self) -> int:
        return len(self._seen_keys)

    @property
    def seen_keys(self) -> tuple[str, ...]:
        return tuple(self._seen_keys)

    @property
    def detect_history(self) -> tuple[bool, ...]:
        return tuple(self._detect_history)

    def recognize(self, image: OcrImage, *, detect: bool = True) -> list[OcrToken]:
        validated = _validate_image(image)
        key = self.key_for_image(validated)
        self._seen_keys.append(key)
        self._detect_history.append(detect)

        keyed_tokens = self._tokens_by_key.get(key)
        if keyed_tokens is not None:
            return list(_copy_tokens(keyed_tokens))

        if self._queue_index >= len(self._queued_responses):
            return []

        queued_tokens = self._queued_responses[self._queue_index]
        self._queue_index += 1
        return list(_copy_tokens(queued_tokens))
