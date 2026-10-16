"""Bounded, memory-only OCR reuse for byte-identical local ROI inputs.

Only SHA-256 fingerprints and copied tokens are retained. A hit preserves the
original raw confidence; it is not another observation or stronger evidence.
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from threading import RLock
from typing import NamedTuple, Protocol

import numpy as np

from wuwa_assistant.models import OcrToken

from .base import OcrImage, _validate_image


class Recognize(Protocol):
    def __call__(self, image: OcrImage, *, detect: bool = True) -> list[OcrToken]: ...


class CacheInfo(NamedTuple):
    hits: int
    misses: int
    entries: int
    token_bytes: int


type _Key = tuple[tuple[int, ...], bool, bytes]
type _Token = tuple[str, float, tuple[int, int, int, int]]


class ExactOcrCache:
    """Session cache with count/size bounds and nonblocking generation invalidation."""

    def __init__(
        self, recognize: Recognize, *, max_entries: int = 64, max_bytes: int = 1_048_576
    ) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("cache bounds must be nonnegative")
        self._recognize = recognize
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[_Key, tuple[tuple[_Token, ...], int]] = OrderedDict()
        self._lock = RLock()
        self._generation = 0
        self._bytes = 0
        self._hits = 0
        self._misses = 0

    def clear(self) -> None:
        """Invalidate even an in-flight inference without waiting on its native call."""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._bytes = 0

    def info(self) -> CacheInfo:
        with self._lock:
            return CacheInfo(self._hits, self._misses, len(self._entries), self._bytes)

    def recognize(self, image: OcrImage, *, detect: bool = True) -> list[OcrToken]:
        validated = _validate_image(image)
        if not self._max_entries or validated.shape[0] * validated.shape[1] > 1_000_000:
            return self._recognize(validated, detect=detect)
        contiguous = np.ascontiguousarray(validated)
        key = (tuple(contiguous.shape), detect, sha256(memoryview(contiguous)).digest())
        with self._lock:
            generation = self._generation
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return [OcrToken(*token) for token in found[0]]
            self._misses += 1
        tokens = self._recognize(validated, detect=detect)
        saved = tuple((token.text, token.confidence, token.bbox) for token in tokens)
        # Conservative Unicode/tuple estimate; neither arrays nor image bytes survive.
        size = 256 + sum(160 + 4 * len(token[0]) for token in saved)
        with self._lock:
            if generation == self._generation and size <= self._max_bytes:
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._bytes -= previous[1]
                self._entries[key] = (saved, size)
                self._bytes += size
                while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
                    _, (_, removed_size) = self._entries.popitem(last=False)
                    self._bytes -= removed_size
        return tokens
