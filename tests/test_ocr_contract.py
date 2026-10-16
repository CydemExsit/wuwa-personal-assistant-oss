from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest

from wuwa_assistant.models import OcrToken
from wuwa_assistant.ocr import (
    FakeOcrEngine,
    OcrBackendError,
    OcrEngine,
    OcrImage,
    RapidOcrEngine,
)


class StubRapidBackend:
    def __init__(self, output: object) -> None:
        self.output = output
        self.calls: list[tuple[OcrImage, dict[str, object]]] = []

    def __call__(self, image: OcrImage, **kwargs: object) -> object:
        self.calls.append((image, kwargs))
        return self.output


def test_fake_engine_supports_keyed_then_queued_deterministic_responses() -> None:
    keyed_image = np.full((4, 5, 3), 17, dtype=np.uint8)
    queued_image = np.zeros((4, 5, 3), dtype=np.uint8)
    keyed_token = OcrToken("生命", 0.97, (1, 1, 4, 3))
    queued_token = OcrToken("4.5%", 0.91, (0, 0, 5, 4))
    keyed_key = FakeOcrEngine.key_for_image(keyed_image)
    queued_key = FakeOcrEngine.key_for_image(queued_image)
    engine = FakeOcrEngine(
        queued_responses=[[queued_token]],
        tokens_by_key={keyed_key: [keyed_token]},
    )

    assert isinstance(engine, OcrEngine)
    first = engine.recognize(keyed_image, detect=False)
    first[0].text = "mutated by a parser test"

    assert engine.recognize(keyed_image) == [keyed_token]
    assert engine.recognize(queued_image) == [queued_token]
    assert engine.recognize(queued_image) == []
    assert engine.call_count == 4
    assert engine.seen_keys == (keyed_key, keyed_key, queued_key, queued_key)
    assert engine.detect_history == (False, True, True, True)


@pytest.mark.parametrize(
    ("bad_image", "error_type"),
    [
        ("https://example.invalid/screenshot.png", TypeError),
        (np.zeros((2, 2), dtype=np.float32), TypeError),
        (np.zeros((0, 2), dtype=np.uint8), ValueError),
        (np.zeros((2, 2, 2), dtype=np.uint8), ValueError),
    ],
)
def test_engines_reject_non_roi_inputs_before_backend_initialization(
    bad_image: object,
    error_type: type[Exception],
) -> None:
    factory_calls = 0

    def factory() -> StubRapidBackend:
        nonlocal factory_calls
        factory_calls += 1
        return StubRapidBackend(None)

    rapid_engine = RapidOcrEngine(engine_factory=factory)
    fake_engine = FakeOcrEngine()

    with pytest.raises(error_type):
        rapid_engine.recognize(cast(OcrImage, bad_image))
    with pytest.raises(error_type):
        fake_engine.recognize(cast(OcrImage, bad_image))
    assert factory_calls == 0
    assert not rapid_engine.is_initialized


def test_rapidocr_is_lazy_and_preserves_raw_scores_and_detector_boxes() -> None:
    output = SimpleNamespace(
        txts=("共鳴技能傷害加成", "7.1%"),
        scores=(0.987654321, np.float32(0.8765432)),
        boxes=np.asarray(
            [
                [[2.2, 3.8], [16.1, 3.1], [16.4, 8.2], [2.0, 8.7]],
                [[-2.0, 9.0], [25.0, 9.0], [25.0, 14.5], [-2.0, 14.5]],
            ],
            dtype=np.float32,
        ),
    )
    backend = StubRapidBackend(output)
    factory_calls = 0

    def factory() -> StubRapidBackend:
        nonlocal factory_calls
        factory_calls += 1
        return backend

    engine = RapidOcrEngine(engine_factory=factory)
    image = np.zeros((12, 20, 3), dtype=np.uint8)

    assert not engine.is_initialized
    assert factory_calls == 0

    tokens = engine.recognize(image)

    assert engine.is_initialized
    assert factory_calls == 1
    assert tokens == [
        OcrToken("共鳴技能傷害加成", 0.987654321, (2, 3, 17, 9)),
        OcrToken("7.1%", float(np.float32(0.8765432)), (0, 9, 20, 12)),
    ]
    called_image, kwargs = backend.calls[0]
    assert isinstance(called_image, np.ndarray)
    assert kwargs == {
        "use_det": True,
        "use_cls": False,
        "use_rec": True,
        "text_score": 0.0,
    }


def test_recognition_only_uses_full_roi_bbox_and_reuses_initialized_backend() -> None:
    output = SimpleNamespace(txts=("+15",), scores=(0.995,))
    backend = StubRapidBackend(output)
    factory_calls = 0

    def factory() -> StubRapidBackend:
        nonlocal factory_calls
        factory_calls += 1
        return backend

    engine = RapidOcrEngine(engine_factory=factory)
    image = np.zeros((18, 72), dtype=np.uint8)

    assert engine.recognize(image, detect=False) == [OcrToken("+15", 0.995, (0, 0, 72, 18))]
    assert engine.recognize(image, detect=False) == [OcrToken("+15", 0.995, (0, 0, 72, 18))]
    assert len(backend.calls) == 1
    assert engine.cache_info.hits == 1
    engine.clear_cache()
    assert engine.recognize(image, detect=False) == [OcrToken("+15", 0.995, (0, 0, 72, 18))]
    assert factory_calls == 1
    assert len(backend.calls) == 2


def test_empty_backend_result_is_an_empty_token_list() -> None:
    image = np.zeros((4, 4), dtype=np.uint8)

    assert RapidOcrEngine(engine_factory=lambda: StubRapidBackend(None)).recognize(image) == []
    no_text = SimpleNamespace(txts=None, scores=None, boxes=None)
    assert RapidOcrEngine(engine_factory=lambda: StubRapidBackend(no_text)).recognize(image) == []


@pytest.mark.parametrize(
    "output",
    [
        SimpleNamespace(txts=("生命",), scores=()),
        SimpleNamespace(txts=("生命",), scores=(0.9,), boxes=None),
        SimpleNamespace(txts=("生命",), scores=(1.1,), boxes=np.zeros((1, 4, 2))),
        SimpleNamespace(txts=(None,), scores=(0.9,), boxes=np.zeros((1, 4, 2))),
        SimpleNamespace(txts=("生命",), scores=(0.9,), boxes=np.zeros((1, 2, 2))),
    ],
)
def test_malformed_backend_output_fails_closed(output: object) -> None:
    engine = RapidOcrEngine(engine_factory=lambda: StubRapidBackend(output))

    with pytest.raises(OcrBackendError):
        engine.recognize(np.zeros((5, 8), dtype=np.uint8))


def test_small_ui_crops_avoid_document_upscaling_without_changing_stat_panels() -> None:
    from wuwa_assistant.ocr.rapidocr_engine import _RoiSizedBackend

    class SizedBackend(StubRapidBackend):
        def __init__(self) -> None:
            super().__init__(None)
            self.text_det = SimpleNamespace(limit_type="min")
            self.sizing: list[str] = []

        def __call__(self, image: OcrImage, **kwargs: object) -> object:
            self.sizing.append(self.text_det.limit_type)
            return super().__call__(image, **kwargs)

    backend = SizedBackend()
    engine = RapidOcrEngine(engine_factory=lambda: _RoiSizedBackend(backend))
    for height, width in ((55, 170), (100, 285), (95, 500), (110, 260), (550, 620), (90, 680)):
        image = np.zeros((height, width, 3), dtype=np.uint8)
        engine.recognize(image)
        assert backend.calls[-1][0] is image
        assert backend.text_det.limit_type == "min"
    engine.recognize(np.zeros((55, 170, 3), dtype=np.uint8), detect=False)
    engine.recognize(np.zeros((55, 900, 3), dtype=np.uint8))
    assert backend.sizing == ["max", "max", "max", "max", "min", "max", "min", "min"]


def test_detector_sizing_is_restored_after_backend_failure() -> None:
    from wuwa_assistant.ocr.rapidocr_engine import _RoiSizedBackend

    class FailingBackend:
        text_det = SimpleNamespace(limit_type="min")

        def __call__(self, image: OcrImage, **kwargs: object) -> object:
            assert self.text_det.limit_type == "max"
            raise OcrBackendError("detector failed")

    backend = FailingBackend()
    engine = RapidOcrEngine(engine_factory=lambda: _RoiSizedBackend(backend))
    with pytest.raises(OcrBackendError, match="detector failed"):
        engine.recognize(np.zeros((55, 170, 3), dtype=np.uint8))
    assert backend.text_det.limit_type == "min"


@pytest.mark.parametrize(
    ("available_cpus", "expected_threads"), [(None, 1), (1, 1), (2, 2), (24, 4)]
)
def test_local_backend_bounds_cpu_parallelism_and_keeps_explicit_offline_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_cpus: int | None,
    expected_threads: int,
) -> None:
    from wuwa_assistant.ocr import rapidocr_engine

    model_directory = tmp_path / "models"
    model_directory.mkdir()
    for filename in RapidOcrEngine._MODEL_FILENAMES.values():
        (model_directory / filename).touch()
    supplied: dict[str, object] = {}

    def factory(*, params: dict[str, object]) -> StubRapidBackend:
        supplied.update(params)
        return StubRapidBackend(None)

    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        SimpleNamespace(__file__=str(tmp_path / "__init__.py"), RapidOCR=factory),
    )
    monkeypatch.setattr(rapidocr_engine, "cpu_count", lambda: available_cpus)
    RapidOcrEngine._create_local_engine()
    assert supplied["EngineConfig.onnxruntime.intra_op_num_threads"] == expected_threads
    assert supplied["EngineConfig.onnxruntime.inter_op_num_threads"] == 1
    for parameter, filename in RapidOcrEngine._MODEL_FILENAMES.items():
        assert supplied[parameter] == str(model_directory / filename)
