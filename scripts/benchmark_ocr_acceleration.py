"""Uncached local OCR comparison; reports hashes, never screenshots or recognized text.

Run CPU and DirectML sequentially on the same machine/runtime/sample directory.
The boundary is classification + parsing, excluding capture, stability and scoring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

import cv2
import numpy as np
from benchmark_performance import process_memory

from wuwa_assistant.models import to_jsonable
from wuwa_assistant.ocr import RapidOcrEngine
from wuwa_assistant.parsers import EchoPageParser
from wuwa_assistant.vision.classifier import PageClassifier


def semantic(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: semantic(item)
            for key, item in value.items()
            if "confidence" not in key and key not in {"raw_ocr", "evidence", "debug_roi_paths"}
        }
    if isinstance(value, list):
        return [semantic(item) for item in value]
    return value


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceleration", choices=("cpu", "directml", "auto"), required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    paths = sorted(args.samples.glob("*.png"))
    if not paths or args.rounds < 2:
        parser.error("provide PNG samples and at least two rounds")
    engine = RapidOcrEngine(acceleration=args.acceleration, cache_entries=0)
    classifier, echo_parser = PageClassifier(engine), EchoPageParser(engine)
    rows = []
    for cycle in range(args.rounds):
        for index, path in enumerate(paths):
            pixels = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if pixels is None:
                raise ValueError(f"sample {index} is unreadable")
            start, cpu_start = time.perf_counter(), time.process_time()
            classification = classifier.classify(pixels)
            classified = time.perf_counter()
            observation = echo_parser.parse(
                pixels, classification, source_file="sample", source_timestamp=None
            )
            ended = time.perf_counter()
            payload = to_jsonable(observation)
            rows.append(
                {
                    "cycle": cycle,
                    "sample": index,
                    "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "semantic_sha256": digest(semantic(payload)),
                    "raw_sha256": digest(payload),
                    "total_ms": (ended - start) * 1000,
                    "classify_ms": (classified - start) * 1000,
                    "parse_ms": (ended - classified) * 1000,
                    "cpu_ms": (time.process_time() - cpu_start) * 1000,
                }
            )
    report = {
        "acceleration": args.acceleration,
        "providers": engine.execution_providers,
        "cache_entries": 0,
        "rows": rows,
        "memory": process_memory(),
        "warm_median_ms": statistics.median(row["total_ms"] for row in rows if row["cycle"]),
        "warm_cpu_median_ms": statistics.median(row["cpu_ms"] for row in rows if row["cycle"]),
    }
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        expected = {(r["cycle"], r["sample"]): r for r in baseline["rows"]}
        assert len(expected) == len(rows), "baseline workload differs"
        mismatches = [
            [r["cycle"], r["sample"]]
            for r in rows
            if any(
                expected[(r["cycle"], r["sample"])][key] != r[key]
                for key in ("input_sha256", "semantic_sha256")
            )
        ]
        report["semantic_parity"] = not mismatches
        report["mismatches"] = mismatches
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    if report.get("semantic_parity") is False:
        raise SystemExit("OCR semantic parity failed")


if __name__ == "__main__":
    main()
