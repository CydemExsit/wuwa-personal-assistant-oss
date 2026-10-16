"""Measure the full local data path with existing private screenshots and a temporary DB.

This is deterministic offline replay, not a measurement of the Windows capture
driver or the intentional 600 ms stability wait. Neither is bypassed in production.
Reports retain metadata/hashes only. No screen capture or original database writes.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import statistics
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from benchmark_performance import digest, measure, process_memory


def replay(samples: Path, rounds: int) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import cv2
    import numpy as np
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from wuwa_assistant.application.echo_history import EchoHistoryService
    from wuwa_assistant.application.scoring_profiles import ScoringProfileService
    from wuwa_assistant.application.scoring_workflow import ScoringWorkflowService
    from wuwa_assistant.capture import CapturedFrame, FrameLeaseAudit, RoiExtractor
    from wuwa_assistant.live.analyzer import LiveEchoRoiAnalyzer
    from wuwa_assistant.live.coordinator import _packet_fingerprint
    from wuwa_assistant.ocr import RapidOcrEngine
    from wuwa_assistant.scoring import load_template_seed_definitions
    from wuwa_assistant.storage import AppPaths, create_database
    from wuwa_assistant.ui.pages.echo_live import (
        EchoLivePage,
        EchoObservationView,
        RecommendationView,
    )
    from wuwa_assistant.ui.theme import apply_dark_theme

    images = [
        cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        for p in sorted(samples.glob("*.png"))
    ]
    if not images or any(image is None for image in images):
        raise ValueError("samples must contain readable PNG files")
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    page = EchoLivePage()
    page.resize(1300, 800)
    page.show()
    app.processEvents()
    engine = RapidOcrEngine()
    analyzer = LiveEchoRoiAnalyzer(engine)
    extractor = RoiExtractor()
    audit = FrameLeaseAudit()
    stages = {
        key: []
        for key in (
            "replay_frame",
            "extract",
            "fingerprint",
            "probe",
            "analyze",
            "persist",
            "score",
            "ui",
            "total",
        )
    }
    signatures = []
    resources = []
    active_cpu_started = time.process_time()
    active_wall_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="wuwa-replay-") as directory:
        database = create_database(AppPaths.from_root(Path(directory)))
        try:
            history = EchoHistoryService(database.session_factory)
            data_set_id = history.ensure_active_primary_dataset()
            profiles = ScoringProfileService(database.session_factory)
            seeded = profiles.ensure_system_templates(load_template_seed_definitions())
            template = next(p for p in seeded if p.template_kind == "generic_dps")
            profile = profiles.clone_template(
                template.id, data_set_id=data_set_id, name="Benchmark"
            )
            workflow = ScoringWorkflowService(database.session_factory, profiles)
            capture = history.start_capture(
                backend_kind="offline_replay",
                monitor_index=1,
                sampling_hz=5,
                stable_wait_ms=600,
                app_version="benchmark",
                data_set_id=data_set_id,
                scoring_profile_id=profile.id,
            )

            def timed(name, operation):
                timing, value = measure(operation, 1)
                stages[name].append(timing["median_ms"])
                return value

            for cycle in range(rounds):
                cycle_signatures = []
                for index, pixels in enumerate(images):
                    sequence = cycle * len(images) + index
                    started = time.perf_counter()
                    frame = timed(
                        "replay_frame",
                        lambda pixels=pixels, sequence=sequence: CapturedFrame(
                            pixels.copy(),
                            "2026-10-14T00:00:00+00:00",
                            1,
                            sequence=sequence,
                            audit=audit,
                        ),
                    )
                    extracted = timed("extract", lambda frame=frame: extractor.extract(frame))
                    probe = timed(
                        "probe", lambda extracted=extracted: analyzer.analyze(extracted.probe)
                    )
                    if not probe.relevant:
                        continue
                    # A production content packet arrives after the stability gate.
                    content_frame = CapturedFrame(
                        pixels, "2026-10-14T00:00:00+00:00", 1, sequence=sequence, audit=audit
                    )
                    extracted = timed(
                        "extract",
                        lambda content_frame=content_frame, probe=probe: extractor.extract(
                            content_frame, page_type=probe.page_type
                        ),
                    )
                    packet = extracted.content
                    assert packet is not None
                    timed("fingerprint", lambda packet=packet: _packet_fingerprint(packet))
                    analysis = timed("analyze", lambda packet=packet: analyzer.analyze(packet))
                    assert analyzer.finalize_analysis(analysis, accept=True)
                    payload = analysis.observation
                    receipt = timed(
                        "persist",
                        lambda payload=payload, sequence=sequence: history.ingest_live_result(
                            capture.id, payload, source_sequence=sequence
                        ),
                    )
                    evaluation = timed(
                        "score",
                        lambda payload=payload: workflow.evaluate(
                            payload,
                            data_set_id=data_set_id,
                            profile_id=profile.id,
                            persist=False,
                        ),
                    )

                    def update_ui(receipt=receipt, payload=payload, evaluation=evaluation):
                        if receipt.echo_session_id is None or receipt.observation_id is None:
                            app.processEvents()
                            return
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
                                opened_slot_quality_score=evaluation.breakdown.opened_slot_quality_score,
                            )
                        )
                        app.processEvents()

                    timed("ui", update_ui)
                    stages["total"].append((time.perf_counter() - started) * 1000)
                    cycle_signatures.append(
                        {
                            "sample": index,
                            "observation": digest(payload),
                            "score": evaluation.breakdown.score,
                            "projection": evaluation.projection.to_dict(),
                            "decision": evaluation.decision.action.value,
                        }
                    )
                signatures.append(cycle_signatures)
                gc.collect()
                resources.append(
                    {
                        "cycle": cycle,
                        **process_memory(),
                        "python_threads": threading.active_count(),
                        "retained_sessions": len(analyzer.tracker.sessions),
                        "retained_events": len(analyzer.tracker.events),
                        "ocr_cache": (
                            engine.cache_info._asdict() if hasattr(engine, "cache_info") else None
                        ),
                    }
                )
            history.stop_capture(capture.id)
            with database.engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
                assert not connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        finally:
            analyzer.stop()
            page.close()
            page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            database.dispose()
    audit.assert_all_released()
    active_cpu = time.process_time() - active_cpu_started
    active_wall = time.perf_counter() - active_wall_started
    idle_cpu = time.process_time()
    time.sleep(1)
    idle_cpu = time.process_time() - idle_cpu
    return {
        "stages": {
            k: {
                "samples_ms": values,
                "median_ms": statistics.median(values),
                "p95_ms": sorted(values)[math.ceil(len(values) * 0.95) - 1],
            }
            for k, values in stages.items()
            if values
        },
        "signatures": signatures,
        "resources": resources,
        "active_cpu_seconds": active_cpu,
        "active_wall_seconds": active_wall,
        "leases": asdict(audit.snapshot),
        "idle_cpu_seconds_per_second": idle_cpu,
        "intentional_stability_wait_ms": 600,
        "database_integrity": True,
        "scope": "offline replay with forced per-input persistence/scoring and real Qt updates",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("rounds must be positive")
    result = replay(args.samples, args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Replay verified: {args.output}")


if __name__ == "__main__":
    main()
