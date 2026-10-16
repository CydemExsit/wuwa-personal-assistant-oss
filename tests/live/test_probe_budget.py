from wuwa_assistant.live import FakeRoiAnalyzer, LiveRecognitionCoordinator

from .helpers import accepted_result, packet


def test_changed_non_echo_pages_cannot_bypass_probe_backoff() -> None:
    analyzer = FakeRoiAnalyzer({}, default=accepted_result(relevant=False))
    coordinator = LiveRecognitionCoordinator(analyzer)
    token = coordinator.start()
    try:
        for sequence, timestamp in enumerate((0.0, 0.3, 0.7), 1):
            coordinator.submit(token, packet(sequence, timestamp, "outside"))
        assert coordinator.wait_until_idle()
        assert coordinator.stats.analysis_started == 1
        for sequence, timestamp in enumerate((0.8, 1.1, 1.5, 1.8, 2.2, 2.6), 4):
            coordinator.submit(token, packet(sequence, timestamp, "another-scene"))
        assert coordinator.wait_until_idle()
        assert coordinator.stats.analysis_started == 1
        for sequence, timestamp in enumerate((2.8, 3.1, 3.5), 10):
            coordinator.submit(token, packet(sequence, timestamp, "another-scene"))
        assert coordinator.wait_until_idle()
        assert coordinator.stats.analysis_started == 2
    finally:
        coordinator.stop()
