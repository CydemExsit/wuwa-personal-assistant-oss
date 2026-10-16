from __future__ import annotations

from threading import Event, Thread

import numpy as np
import pytest

from wuwa_assistant.models import OcrToken
from wuwa_assistant.ocr.cache import ExactOcrCache


def test_exact_pixels_mode_shape_and_defensive_token_copy() -> None:
    calls = []

    def recognize(image, *, detect=True):
        calls.append((image.shape, detect))
        return [OcrToken("低信心", 0.3, (0, 0, 2, 2))]

    cache = ExactOcrCache(recognize)
    pixels = np.zeros((3, 4, 3), dtype=np.uint8)
    first = cache.recognize(pixels)
    first[0].text = "changed"
    first.clear()
    cached = cache.recognize(pixels.copy())
    assert cached[0].text == "低信心" and cached[0].confidence == 0.3
    cached[0].confidence = 1.0
    assert cache.recognize(pixels)[0].confidence == 0.3
    assert len(calls) == 1
    cache.recognize(pixels, detect=False)
    cache.recognize(pixels.reshape(4, 3, 3))
    pixels[0, 0, 0] = 1
    cache.recognize(pixels)
    assert len(calls) == 4


def test_cache_eviction_empty_results_and_input_validation() -> None:
    calls = []

    def recognize(image, *, detect=True):
        calls.append(int(image[0, 0]))
        return []

    cache = ExactOcrCache(recognize, max_entries=2, max_bytes=512)
    for value in (1, 2, 1, 3, 2):
        cache.recognize(np.full((3, 4), value, dtype=np.uint8))
    assert calls == [1, 2, 3, 2]
    assert cache.info().entries == 2 and cache.info().token_bytes <= 512
    with pytest.raises(TypeError):
        cache.recognize(np.zeros((3, 4), dtype=np.float64))
    cache.clear()
    assert cache.info().entries == cache.info().token_bytes == 0


def test_clear_during_inference_does_not_block_or_repopulate_cache() -> None:
    started, finish = Event(), Event()

    def recognize(image, *, detect=True):
        started.set()
        assert finish.wait(5)
        return [OcrToken("text", 0.9, (0, 0, 2, 2))]

    cache = ExactOcrCache(recognize)
    worker = Thread(target=lambda: cache.recognize(np.zeros((3, 4), dtype=np.uint8)))
    worker.start()
    try:
        assert started.wait(5)
        cache.clear()
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()
    assert cache.info().entries == 0


def test_live_lifecycle_clears_pixel_cache_without_reinitializing_model() -> None:
    from wuwa_assistant.live.analyzer import LiveEchoRoiAnalyzer

    cache = ExactOcrCache(lambda image, *, detect=True: [])

    # The live boundary supports engines that expose an explicit cache lifecycle.
    class Engine:
        recognize = cache.recognize
        clear_cache = cache.clear

    analyzer = LiveEchoRoiAnalyzer(Engine())
    for lifecycle in (analyzer.reset, analyzer.request_recheck, analyzer.stop):
        cache.recognize(np.zeros((3, 4), dtype=np.uint8))
        assert cache.info().entries == 1
        lifecycle()
        assert cache.info().entries == 0
