"""Repeatable local benchmarks. Never captures a screen or opens the user's database.

Run each suite in a fresh process on the same machine before and after a change.
Private sample images are optional; reports contain hashes, not OCR text/images.
Timings are observations, never machine-dependent CI assertions.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any


def process_memory() -> dict[str, int]:
    """Read only this benchmark process's working set; no process discovery."""
    if sys.platform != "win32":
        return {}

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("faults", ctypes.c_ulong),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working",
                    "working",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                    "private",
                )
            ],
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"rss_bytes": counters.working, "private_bytes": counters.private}


def measure(work: Callable[[], Any], repeats: int) -> tuple[dict[str, Any], Any]:
    samples = []
    cpu = time.process_time()
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = work()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "cpu_seconds": time.process_time() - cpu,
        **process_memory(),
    }, result


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def scoring(repeats: int) -> dict[str, Any]:
    from wuwa_assistant.scoring import EchoScoringEngine, ObservedStat, load_scoring_catalog
    from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION

    catalog = load_scoring_catalog()
    stats = tuple(
        ObservedStat(name=name, value=value, unit=unit, confidence=0.99)
        for name, value, unit in (
            ("暴擊", 7.5, "%"),
            ("攻擊", 10.1, "%"),
            ("共鳴效率", 10.0, "%"),
            ("暴擊傷害", 15.0, "%"),
            ("攻擊", 40, ""),
        )
    )
    results = {}
    current = catalog.profile("generic_dps_v1")
    legacy = replace(
        current, profile_id="generic_dps_legacy", roll_data_version=LEGACY_ROLL_VERSION
    )
    for profile in (current, legacy):
        engine = EchoScoringEngine(catalog.rolls)
        for opened in (1, 2, 3, 4, 5):

            def run(engine: Any = engine, profile: Any = profile, opened: int = opened) -> Any:
                return engine.enumerate_remaining(profile, stats[:opened], improvement_target=80.0)

            cold, projection = measure(run, 1)
            warm, repeated = measure(run, repeats)
            assert projection == repeated
            results[f"{profile.profile_id}/+{opened * 5}"] = {
                "cold": cold,
                "repeat": warm,
                "result": projection.to_dict(),
            }
    return results


def recognition(repeats: int, samples: Path | None) -> dict[str, Any]:
    import cv2
    import numpy as np

    from wuwa_assistant.models import to_jsonable
    from wuwa_assistant.ocr import RapidOcrEngine
    from wuwa_assistant.parsers import EchoPageParser
    from wuwa_assistant.vision.classifier import PageClassifier

    engine = RapidOcrEngine()
    synthetic = np.full((64, 240, 3), 255, dtype=np.uint8)
    cv2.putText(synthetic, "TEST", (20, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    cold, tokens = measure(lambda: engine.recognize(synthetic), 1)
    warm, _ = measure(lambda: engine.recognize(synthetic), repeats)
    results: dict[str, Any] = {
        "synthetic": {"cold": cold, "repeat": warm, "token_count": len(tokens)}
    }
    if samples is None:
        return results
    parser = EchoPageParser(engine)
    classifier = PageClassifier(engine)
    for index, path in enumerate(sorted(samples.glob("*.png"))):
        frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError(f"unreadable sample {index}")
        stages: dict[str, list[float]] = {"classification_ms": [], "parse_ms": []}
        hashes: set[str] = set()

        def run(frame: Any = frame, stages: Any = stages, hashes: Any = hashes) -> None:
            started = time.perf_counter()
            classification = classifier.classify(frame)
            stages["classification_ms"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            observation = parser.parse(
                frame, classification, source_file="sample", source_timestamp=None
            )
            stages["parse_ms"].append((time.perf_counter() - started) * 1000)
            hashes.add(digest(to_jsonable(observation)))

        first, _ = measure(run, 1)
        repeated, _ = measure(run, repeats)
        assert len(hashes) == 1, f"nondeterministic OCR output for sample {index}"
        results[f"sample_{index:02d}"] = {
            "cold": first,
            "repeat": repeated,
            "stages": stages,
            "output_sha256": next(iter(hashes)),
        }
    return results


def ui(repeats: int) -> dict[str, Any]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QWidget

    from wuwa_assistant.ui.pages.equipment_overview import (
        EquipmentCharacterView,
        EquipmentOverviewPage,
        EquipmentPieceView,
    )
    from wuwa_assistant.ui.theme import apply_dark_theme

    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    page = EquipmentOverviewPage()
    page.resize(1300, 800)
    page.show()
    rows = tuple(
        EquipmentCharacterView(
            identifier=str(index),
            name=f"測試角色 {index}",
            element="冷凝",
            average=60.0,
            total=300.0,
            scored_count=5,
            equipped_count=5,
            level_done=True,
            weapon_done=True,
            skills_done=True,
            echoes_done=True,
            pieces=tuple(
                EquipmentPieceView(
                    echo_id=f"{index}-{slot}",
                    character_id=str(index),
                    character_name=f"測試角色 {index}",
                    name="測試聲骸",
                    slot=slot,
                    level=25,
                    set_name="測試套裝",
                    score=60.0,
                )
                for slot in range(1, 6)
            ),
        )
        for index in range(50)
    )

    def drain() -> None:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def render(data: Any = rows) -> None:
        page.apply_data(
            data,
            tuple(p for row in data for p in row.pieces),
            profiles=(),
            profile_id=None,
            summary="benchmark",
        )
        drain()

    initial, _ = measure(render, 1)
    unchanged, _ = measure(render, repeats)
    changed = (replace(rows[0], total=301.0), *rows[1:])
    single, _ = measure(lambda: render(changed), 1)
    toggle, _ = measure(
        lambda: (page.view_combo.setCurrentIndex(1 - page.view_combo.currentIndex()), drain()),
        repeats,
    )
    before = process_memory()
    sustained, _ = measure(render, 50)
    gc.collect()
    after = process_memory()
    result = {
        "initial": initial,
        "unchanged": unchanged,
        "single_change": single,
        "toggle": toggle,
        "sustained": sustained,
        "memory_before": before,
        "memory_after": after,
        "live_widgets": len(page.findChildren(QWidget)),
        "python_threads": threading.active_count(),
    }
    page.close()
    page.deleteLater()
    drain()
    return result


def startup(repeats: int, executable: Path | None) -> dict[str, Any]:
    command = (
        [str(executable)] if executable else [sys.executable, "-B", "-m", "wuwa_assistant.app"]
    )
    with tempfile.TemporaryDirectory(prefix="wuwa-benchmark-") as directory:

        def run() -> None:
            subprocess.run(
                [*command, "--smoke-test", "--data-root", directory],
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                check=True,
                capture_output=True,
                timeout=60,
            )

        first, _ = measure(run, 1)
        repeated, _ = measure(run, repeats)
    # process_time/GetProcessMemoryInfo describe this benchmark driver, not its
    # child application. Report only wall-clock latency for subprocess startup.
    for timing in (first, repeated):
        for name in ("cpu_seconds", "rss_bytes", "private_bytes"):
            timing.pop(name, None)
    return {
        "new_database": first,
        "existing_database": repeated,
        "scope": "isolated process smoke startup, no capture or presenter initialization",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("scoring", "recognition", "ui", "startup"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--samples", type=Path)
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    suites = {
        "scoring": lambda: scoring(args.repeats),
        "recognition": lambda: recognition(args.repeats, args.samples),
        "ui": lambda: ui(args.repeats),
        "startup": lambda: startup(args.repeats, args.executable),
    }
    report = {
        "schema": 1,
        "suite": args.suite,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "repeats": args.repeats,
        "results": suites[args.suite](),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Completed {args.suite}: {args.output}")


if __name__ == "__main__":
    main()
