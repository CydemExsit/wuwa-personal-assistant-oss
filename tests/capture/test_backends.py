from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wuwa_assistant.capture import (
    CaptureConfig,
    CapturedFrame,
    CaptureExhaustedError,
    CaptureStatus,
    FakeCaptureBackend,
    FrameLeaseAudit,
    FrameReleasedError,
    ImageSequenceBackend,
    InvalidMonitorError,
    MonitorInfo,
)


def reference_frame(value: int = 0) -> np.ndarray:
    return np.full((1440, 2560, 3), value, dtype=np.uint8)


def test_capture_configuration_and_physical_monitor_identity() -> None:
    monitor = MonitorInfo(2, -2560, 0, 2560, 1440, False, "left monitor")
    config = CaptureConfig(monitor_index=monitor.index, sample_hz=5.0)

    assert monitor.stable_key == "-2560:0:2560x1440"
    assert monitor.is_reference_size
    assert monitor.bounds == {"left": -2560, "top": 0, "width": 2560, "height": 1440}
    assert config.interval_seconds == pytest.approx(0.2)
    assert CaptureStatus.NOT_ENABLED.value == "not_enabled"
    assert CaptureStatus.LOW_CONFIDENCE.value == "low_confidence"

    with pytest.raises(ValueError, match="virtual desktop"):
        MonitorInfo(0, 0, 0, 5120, 1440)
    with pytest.raises(ValueError, match="physical monitor"):
        CaptureConfig(monitor_index=0)
    with pytest.raises(ValueError, match="between 2 and 8"):
        CaptureConfig(monitor_index=1, sample_hz=8.1)


def test_frame_lease_is_context_managed_audited_and_pixel_safe_in_repr() -> None:
    audit = FrameLeaseAudit()
    frame = CapturedFrame(reference_frame(17), "2026-08-01T00:00:00+00:00", 1, audit=audit)

    assert audit.snapshot.acquired == 1
    assert audit.snapshot.active == 1
    assert "array" not in repr(frame)
    assert "17" not in repr(frame)

    with frame as active:
        assert active.pixels[0, 0].tolist() == [17, 17, 17]

    assert frame.is_released
    assert audit.snapshot.released == 1
    assert audit.snapshot.active == 0
    audit.assert_all_released()
    frame.release()
    assert audit.snapshot.released == 1
    with pytest.raises(FrameReleasedError):
        _ = frame.pixels


def test_fake_backend_returns_defensive_finite_leases() -> None:
    source = reference_frame(23)
    backend = FakeCaptureBackend([source, source])
    source.fill(99)

    first = backend.capture_monitor(1)
    assert first.pixels[0, 0].tolist() == [23, 23, 23]
    first.pixels.fill(7)
    first.release()

    with backend.capture_monitor(1) as second:
        assert second.pixels[0, 0].tolist() == [23, 23, 23]
        assert second.sequence == 2

    backend.audit.assert_all_released()
    with pytest.raises(CaptureExhaustedError):
        backend.capture_monitor(1)
    with pytest.raises(InvalidMonitorError):
        backend.capture_monitor(0)


def test_image_sequence_preserves_explicit_order_and_never_loops(tmp_path: Path) -> None:
    paths = [tmp_path / "2.png", tmp_path / "1.png"]
    loaded: list[Path] = []

    def loader(path: Path) -> np.ndarray:
        loaded.append(path)
        return reference_frame(10 + len(loaded))

    backend = ImageSequenceBackend(paths, image_loader=loader)

    with backend.capture_monitor(1) as first:
        assert first.source_file == str(paths[0].resolve())
        assert first.pixels[0, 0].tolist() == [11, 11, 11]
    with backend.capture_monitor(1) as second:
        assert second.source_file == str(paths[1].resolve())
        assert second.pixels[0, 0].tolist() == [12, 12, 12]

    assert loaded == [path.resolve() for path in paths]
    with pytest.raises(CaptureExhaustedError):
        backend.capture_monitor(1)
    backend.audit.assert_all_released()
    backend.close()
