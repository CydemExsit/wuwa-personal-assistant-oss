import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wuwa_assistant.ocr import RapidOcrEngine
from wuwa_assistant.ocr.rapidocr_engine import _providers


def test_provider_report_requires_both_active_models() -> None:
    def model(providers: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            session=SimpleNamespace(session=SimpleNamespace(get_providers=lambda: providers))
        )

    engine = SimpleNamespace(
        text_det=model(["DmlExecutionProvider", "CPUExecutionProvider"]),
        text_rec=model(["CPUExecutionProvider"]),
    )
    assert _providers(engine) == ("CPUExecutionProvider",)
    engine.text_rec = engine.text_det
    assert "DmlExecutionProvider" in _providers(engine)


def test_driver_failure_retries_cpu_once_and_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Backend:
        def __init__(self, gpu: bool) -> None:
            self.providers = ("DmlExecutionProvider",) if gpu else ("CPUExecutionProvider",)
            self.gpu = gpu

        def __call__(self, *_args: object, **_kwargs: object) -> object:
            calls.append("gpu" if self.gpu else "cpu")
            if self.gpu:
                raise RuntimeError("device removed")
            return None

    engine = RapidOcrEngine(engine_factory=lambda: Backend(True), cache_entries=0)
    monkeypatch.setattr(engine, "_create_local_engine", lambda **_kwargs: Backend(False))
    image = np.zeros((30, 60, 3), dtype=np.uint8)
    assert engine.recognize(image) == []
    assert engine.recognize(image) == []
    assert calls == ["gpu", "cpu", "cpu"]
    assert engine.execution_providers == ("CPUExecutionProvider",)
    assert engine.fallback_reason == "RuntimeError"


def test_invalid_acceleration_is_rejected_before_loading_models() -> None:
    with pytest.raises(ValueError, match="acceleration"):
        RapidOcrEngine(acceleration="turbo")


@pytest.mark.parametrize("failure", ["creation", "silent_cpu", "none"])
def test_auto_checks_actual_sessions_and_falls_back_if_initialization_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import onnxruntime

    (tmp_path / "models").mkdir()
    for filename in RapidOcrEngine._MODEL_FILENAMES.values():
        (tmp_path / "models" / filename).touch()
    calls = []

    def factory(*, params: dict[str, object]) -> object:
        gpu = bool(params["EngineConfig.onnxruntime.use_dml"])
        calls.append(gpu)
        if gpu and failure == "creation":
            raise RuntimeError("driver unavailable")
        providers = (
            ["DmlExecutionProvider", "CPUExecutionProvider"]
            if gpu and failure == "none"
            else ["CPUExecutionProvider"]
        )
        session = SimpleNamespace(get_providers=lambda: providers, disable_fallback=lambda: None)
        component = SimpleNamespace(session=SimpleNamespace(session=session), limit_type="min")
        assert params["Rec.rec_batch_num"] == (1 if gpu else 6)
        return SimpleNamespace(text_det=component, text_rec=component)

    monkeypatch.setattr(
        onnxruntime,
        "get_available_providers",
        lambda: ["DmlExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        SimpleNamespace(__file__=str(tmp_path / "__init__.py"), RapidOCR=factory),
    )
    backend = RapidOcrEngine._create_local_engine()
    assert ("DmlExecutionProvider" in backend.providers) == (failure == "none")
    assert calls == ([True] if failure == "none" else [True, False])
