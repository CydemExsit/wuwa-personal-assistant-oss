"""Opt-in local MSS capture and isolated live-stability benchmark.

Captures one physical monitor into temporary memory. Never saves, OCRs, displays,
or uploads pixels; only durations and lease counts reach the JSON report.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

from benchmark_performance import process_memory

from wuwa_assistant.capture import MssMonitorCaptureBackend, RoiExtractor
from wuwa_assistant.live import AnalysisResult, LiveRecognitionCoordinator
from wuwa_assistant.live.coordinator import _packet_fingerprint


def summarize(samples: list[float]) -> dict:
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[math.ceil(len(samples) * 0.95) - 1],
    }


def benchmark(monitor: int, repeats: int) -> dict:
    backend = MssMonitorCaptureBackend()
    extractor = RoiExtractor()
    samples = {"physical_capture": [], "roi_extract": [], "fingerprint": []}
    started_cpu = time.process_time()
    try:
        selected = next(m for m in backend.list_monitors() if m.index == monitor)
        if not selected.is_reference_size:
            raise ValueError("selected monitor must be 2560x1440 for the application backend")
        for _ in range(repeats):
            started = time.perf_counter()
            frame = backend.capture_monitor(monitor)
            samples["physical_capture"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            packet = extractor.extract(frame, page_type="echo_selection").content
            assert packet is not None
            samples["roi_extract"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            fingerprint = _packet_fingerprint(packet)
            samples["fingerprint"].append((time.perf_counter() - started) * 1000)
    finally:
        backend.close()
    backend.audit.assert_all_released()
    capture_cpu = time.process_time() - started_cpu
    accepted = Event()
    scheduled_at = []

    class NoOcr:
        def analyze(self, _packet):
            scheduled_at.append(time.monotonic_ns())
            accepted.set()
            return AnalysisResult("echo_selection", True, 0.99, {})

    coordinator = LiveRecognitionCoordinator(
        NoOcr(),
        fingerprint_factory=lambda _: fingerprint,
        stable_duration_seconds=0.6,
        minimum_stable_samples=3,
    )
    token = coordinator.start()
    decision_ms = []
    started = time.monotonic()
    first_ns = None
    try:
        for sequence in range(10):
            if accepted.is_set():
                break
            if sequence:
                time.sleep(max(0, started + sequence * 0.2 - time.monotonic()))
            current_ns = time.monotonic_ns()
            if first_ns is None:
                first_ns = current_ns
            timed_packet = replace(packet, sequence=sequence, monotonic_timestamp_ns=current_ns)
            started_call = time.perf_counter()
            coordinator.submit(token, timed_packet)
            decision_ms.append((time.perf_counter() - started_call) * 1000)
        assert accepted.wait(2), "stable input was not scheduled"
        assert first_ns is not None
        observed_wait = (scheduled_at[0] - first_ns) / 1_000_000
        # Exercise duplicate suppression without fingerprint work or extra OCR.
        first_duplicate = sequence + 1
        for sequence in range(first_duplicate, repeats + first_duplicate):
            timed_packet = replace(
                packet,
                sequence=sequence,
                monotonic_timestamp_ns=current_ns + sequence * 200_000_000,
            )
            started_call = time.perf_counter()
            coordinator.submit(token, timed_packet)
            decision_ms.append((time.perf_counter() - started_call) * 1000)
        assert coordinator.wait_until_idle()
    finally:
        assert coordinator.stop()
    assert coordinator.stats.scheduled == 1
    assert coordinator.stats.duplicate_states_dropped >= repeats
    return {
        "stages": {k: summarize(v) for k, v in samples.items()},
        "stability_decision_without_hash": summarize(decision_ms),
        "configured_stability_wait_ms": 600,
        "observed_stability_wait_ms": observed_wait,
        "coordinator": asdict(coordinator.stats),
        "leases": asdict(backend.audit.snapshot),
        "capture_cpu_seconds": capture_cpu,
        **process_memory(),
        "monitor_dimensions": [selected.width, selected.height],
        "pixels_saved": False,
        "scope": "desktop capture driver; no game OCR or game interaction",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-capture", action="store_true", required=True)
    parser.add_argument("--monitor", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1 or args.monitor < 1:
        parser.error("monitor and repeats must be positive")
    report = benchmark(args.monitor, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
    print(f"Capture/stability verified; no pixels retained: {args.output}")


if __name__ == "__main__":
    main()
