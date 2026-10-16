"""Paced offline soak through real capture scheduling, OCR, SQLite, scoring and Qt.

Uses existing PNGs as in-memory frames and a temporary database. Never captures
the desktop, opens the user's database, or writes images/OCR text into reports.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import queue
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from benchmark_performance import process_memory


def soak(samples: Path, duration: float, output: Path) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import cv2
    import numpy as np
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QWidget

    from wuwa_assistant.application.echo_history import EchoHistoryService
    from wuwa_assistant.application.scoring_profiles import ScoringProfileService
    from wuwa_assistant.application.scoring_workflow import ScoringWorkflowService
    from wuwa_assistant.capture import CaptureConfig, CapturedFrame, FrameLeaseAudit, MonitorInfo
    from wuwa_assistant.live.analyzer import LiveEchoRoiAnalyzer
    from wuwa_assistant.live.runtime import LiveCaptureRuntime
    from wuwa_assistant.ocr import RapidOcrEngine
    from wuwa_assistant.scoring import load_template_seed_definitions
    from wuwa_assistant.storage import AppPaths, create_database
    from wuwa_assistant.ui.pages.echo_live import (
        EchoLivePage,
        EchoObservationView,
        RecommendationView,
    )

    images = [
        cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        for p in sorted(samples.glob("*.png"))
    ]
    if not images or any(image is None for image in images):
        raise ValueError("provide readable PNG samples")
    app = QApplication.instance() or QApplication([])
    page = EchoLivePage()
    page.show()
    app.processEvents()
    initial_threads = threading.active_count()
    model_loads = 0

    def create_model():
        nonlocal model_loads
        model_loads += 1
        return RapidOcrEngine._create_local_engine(
            acceleration=os.environ.get("WUWA_OCR_ACCELERATION", "auto")
        )

    engine = RapidOcrEngine(engine_factory=create_model)
    analyzer = LiveEchoRoiAnalyzer(engine)
    ui_queue = queue.Queue(maxsize=1)
    errors = []
    records = []
    cycles = []
    saved = 0
    started = time.monotonic()
    last_cpu = time.process_time()
    last_sample = started

    class Backend:
        def __init__(self):
            self.audit = FrameLeaseAudit()
            self.sequence = 0

        def list_monitors(self):
            return (MonitorInfo(1, 0, 0, 2560, 1440),)

        def capture_monitor(self, _monitor):
            elapsed = time.monotonic() - started
            index = (
                min(3, len(images) - 1)
                if elapsed < duration * 0.2
                else int(elapsed // 5) % len(images)
            )
            self.sequence += 1
            return CapturedFrame.now(
                images[index].copy(), 1, sequence=self.sequence, audit=self.audit
            )

        def close(self):
            pass

    with tempfile.TemporaryDirectory(prefix="wuwa-soak-") as directory:
        database = create_database(AppPaths.from_root(Path(directory)))
        history = EchoHistoryService(database.session_factory)
        data_set_id = history.ensure_active_primary_dataset()
        profiles = ScoringProfileService(database.session_factory)
        templates = profiles.ensure_system_templates(load_template_seed_definitions())
        template = next(p for p in templates if p.template_kind == "generic_dps")
        profile = profiles.clone_template(template.id, data_set_id=data_set_id, name="Soak")
        workflow = ScoringWorkflowService(database.session_factory, profiles)
        capture = None

        def result_received(result):
            nonlocal saved
            payload = dict(result.analysis.observation)
            if not result.analysis.relevant or "current_session" not in payload:
                return
            receipt = history.ingest_live_result(
                capture.id, payload, source_sequence=result.source_sequence
            )
            evaluation = workflow.evaluate(
                payload, data_set_id=data_set_id, profile_id=profile.id, persist=False
            )
            saved += 1
            if ui_queue.full():
                with suppress(queue.Empty):
                    ui_queue.get_nowait()
            ui_queue.put_nowait((receipt, payload, evaluation))

        def start_cycle():
            nonlocal capture
            capture = history.start_capture(
                backend_kind="offline_soak",
                monitor_index=1,
                sampling_hz=5,
                stable_wait_ms=600,
                app_version="benchmark",
                data_set_id=data_set_id,
                scoring_profile_id=profile.id,
            )
            backend = Backend()
            runtime = LiveCaptureRuntime(
                backend,
                CaptureConfig(1),
                analyzer,
                on_result=result_received,
                on_error=errors.append,
            )
            runtime.start()
            return backend, runtime

        def stop_cycle(backend, runtime):
            assert runtime.stop(timeout=15), "worker failed to stop"
            backend.audit.assert_all_released()
            history.stop_capture(capture.id)
            assert threading.active_count() == initial_threads, "Python worker leaked"
            if hasattr(engine, "cache_info"):
                assert engine.cache_info.entries == 0
            cycles.append(
                {
                    "leases": asdict(backend.audit.snapshot),
                    "coordinator": asdict(runtime.coordinator.stats),
                }
            )

        backend = runtime = None
        try:
            # Real Qt idle phase, before model initialization/capture workers.
            idle_start, idle_cpu = time.monotonic(), time.process_time()
            while time.monotonic() - idle_start < 5:
                app.processEvents()
                time.sleep(0.02)
            idle_cpu = (time.process_time() - idle_cpu) / (time.monotonic() - idle_start)
            started = time.monotonic()
            last_sample, last_cpu = started, time.process_time()
            backend, runtime = start_cycle()
            next_restart = duration * 0.8
            while time.monotonic() - started < duration:
                app.processEvents()
                try:
                    receipt, payload, evaluation = ui_queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if receipt.echo_session_id and receipt.observation_id:
                        page.apply_observation(
                            EchoObservationView(
                                receipt.echo_session_id,
                                receipt.observation_id,
                                payload.get("name"),
                                payload.get("set_name"),
                                payload.get("cost"),
                                payload.get("level"),
                            )
                        )
                        page.apply_recommendation(
                            RecommendationView(
                                score=evaluation.breakdown.score,
                                expected_score=evaluation.projection.expected_score,
                            )
                        )
                now = time.monotonic()
                if now - last_sample >= 15:
                    gc.collect()
                    with runtime.coordinator._condition:
                        pending = int(runtime.coordinator._pending is not None)
                        active = int(runtime.coordinator._active is not None)
                    records.append(
                        {
                            "elapsed_seconds": now - started,
                            **process_memory(),
                            "cpu_seconds_per_second": (time.process_time() - last_cpu)
                            / (now - last_sample),
                            "python_threads": threading.active_count(),
                            "active": active,
                            "pending": pending,
                            "ui_pending": ui_queue.qsize(),
                            "saved_results": saved,
                            "model_loads": model_loads,
                            "qt_widgets": len(page.findChildren(QWidget)),
                            "ocr_cache": engine.cache_info._asdict()
                            if hasattr(engine, "cache_info")
                            else None,
                        }
                    )
                    last_sample, last_cpu = now, time.process_time()
                    output.with_suffix(".progress.json").write_text(
                        json.dumps(records[-1]), encoding="utf8"
                    )
                if now - started >= next_restart:
                    stop_cycle(backend, runtime)
                    backend, runtime = start_cycle()
                    next_restart += max(10, duration * 0.05)
                time.sleep(0.01)
            stop_cycle(backend, runtime)
            runtime = None
            with database.engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
                assert not connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        finally:
            if runtime is not None:
                runtime.stop(timeout=15)
            page.close()
            page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            gc.collect()
            database.dispose()
    assert not errors, [error.error_type for error in errors]
    assert model_loads == 1 and saved > 0
    assert all(r["python_threads"] <= initial_threads + 2 and r["ui_pending"] <= 1 for r in records)
    return {
        "duration_seconds": duration,
        "records": records,
        "cycles": cycles,
        "idle_cpu_seconds_per_second": idle_cpu,
        "model_loads": model_loads,
        "execution_providers": engine.execution_providers,
        "saved_results": saved,
        "database_integrity": True,
        "errors": [],
        "final_python_threads": threading.active_count(),
        "scope": (
            "5 Hz offline runtime; stable scene, rotating scenes, repeated stop/start; "
            "no physical capture"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.duration < 30:
        parser.error("duration must be at least 30 seconds")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = soak(args.samples, args.duration, args.output)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf8")
    print(f"Paced runtime soak verified: {args.output}")


if __name__ == "__main__":
    main()
