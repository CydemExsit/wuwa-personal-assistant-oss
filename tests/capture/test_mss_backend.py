from __future__ import annotations

from collections.abc import Mapping
from queue import Queue
from threading import Thread

import numpy as np
import pytest

from wuwa_assistant.capture import (
    CaptureThreadError,
    FakeCaptureBackend,
    ImageSequenceBackend,
    InvalidMonitorError,
    MssMonitorCaptureBackend,
)


class StubMss:
    def __init__(self) -> None:
        self.monitors: list[dict[str, object]] = [
            {"left": -2560, "top": 0, "width": 5120, "height": 1440},
            {
                "left": 0,
                "top": 0,
                "width": 2560,
                "height": 1440,
                "is_primary": True,
                "name": "PHL 275M8RZ",
                "unique_id": "must-never-be-retained",
            },
            {
                "left": -2560,
                "top": 0,
                "width": 2560,
                "height": 1440,
                "is_primary": False,
                "name": "SA272U E",
                "unique_id": "must-never-be-retained-either",
            },
            {"left": 0, "top": 1440, "width": 1920, "height": 1080},
        ]
        self.grabbed: list[Mapping[str, int]] = []
        self.closed = False

    def grab(self, monitor: Mapping[str, int]) -> np.ndarray:
        self.grabbed.append(monitor)
        pixels = np.empty((monitor["height"], monitor["width"], 4), dtype=np.uint8)
        pixels[:, :, 0] = 11
        pixels[:, :, 1] = 22
        pixels[:, :, 2] = 33
        pixels[:, :, 3] = 255
        return pixels

    def close(self) -> None:
        self.closed = True


def test_mss_is_lazy_excludes_virtual_desktop_and_only_grabs_selected_bounds() -> None:
    contexts: list[StubMss] = []

    def factory() -> StubMss:
        context = StubMss()
        contexts.append(context)
        return context

    backend = MssMonitorCaptureBackend(mss_factory=factory)
    assert contexts == []

    with pytest.raises(InvalidMonitorError, match="virtual desktop"):
        backend.capture_monitor(0)
    assert contexts == []

    monitors = backend.list_monitors()
    assert [monitor.index for monitor in monitors] == [1, 2, 3]
    assert all(monitor.index != 0 for monitor in monitors)
    assert [monitor.name for monitor in monitors] == ["PHL 275M8RZ", "SA272U E", None]
    assert [monitor.is_primary for monitor in monitors] == [True, False, False]
    assert "must-never-be-retained" not in repr(monitors)
    assert len(contexts) == 1

    with backend.capture_monitor(2) as frame:
        assert frame.pixels.shape == (1440, 2560, 3)
        assert frame.pixels.flags.c_contiguous
        assert frame.pixels[0, 0].tolist() == [11, 22, 33]

    assert contexts[0].grabbed == [{"left": -2560, "top": 0, "width": 2560, "height": 1440}]
    backend.audit.assert_all_released()
    with backend.capture_monitor(3) as frame:
        assert frame.pixels.shape == (1080, 1920, 3)
    assert len(contexts[0].grabbed) == 2
    backend.audit.assert_all_released()

    backend.close()
    assert contexts[0].closed


def test_mss_backend_is_bound_to_the_thread_that_created_its_context() -> None:
    context = StubMss()
    backend = MssMonitorCaptureBackend(mss_factory=lambda: context)
    assert backend.list_monitors()
    failures: Queue[BaseException] = Queue()

    def capture_from_other_thread() -> None:
        try:
            backend.capture_monitor(1)
        except BaseException as exc:
            failures.put(exc)

    thread = Thread(target=capture_from_other_thread)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    failure = failures.get_nowait()
    assert isinstance(failure, CaptureThreadError)
    assert context.grabbed == []
    backend.close()


@pytest.mark.parametrize(
    "backend_type", [FakeCaptureBackend, ImageSequenceBackend, MssMonitorCaptureBackend]
)
@pytest.mark.parametrize("forbidden_name", ["save", "shot", "record", "capture_window"])
def test_capture_backends_expose_no_persistence_or_window_api(
    backend_type: type[object],
    forbidden_name: str,
) -> None:
    assert not hasattr(backend_type, forbidden_name)
