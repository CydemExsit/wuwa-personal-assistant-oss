"""Compare an experimental C++ DLL with NumPy, including conversion and Python ties.

The application never imports this prototype. Build projection_native.cpp first;
the same CLI can be packaged separately to measure the frozen boundary.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from benchmark_performance import measure

from wuwa_assistant.scoring import EchoScoringEngine, ObservedStat, load_scoring_catalog
from wuwa_assistant.scoring import engine as engine_module
from wuwa_assistant.scoring.calibration import score_points
from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION
from wuwa_assistant.scoring.projection import score_distribution


class NativeProjection:
    def __init__(self, library: Path) -> None:
        self.library = ctypes.CDLL(str(library.resolve()))
        self.callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double
        )
        self.callback = self.callback_type(
            lambda earned, reference, ideal: round(score_points(earned, reference, ideal) * 100)
        )
        self.function = self.library.projection_mass
        doubles = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
        integers = np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS")
        self.function.argtypes = [
            doubles,
            doubles,
            integers,
            integers,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            self.callback_type,
            doubles,
        ]
        self.function.restype = ctypes.c_int
        self.conversion_ms = []

    def __call__(self, options, remaining, earned, reference, ideal):
        started = time.perf_counter()
        points = np.array([p for row in options for p, _ in row], dtype=np.float64)
        probabilities = np.array([p for row in options for _, p in row], dtype=np.float64)
        sizes = np.array([len(row) for row in options], dtype=np.int32)
        offsets = np.concatenate(
            (np.array([0], dtype=np.int32), np.cumsum(sizes[:-1], dtype=np.int32))
        )
        output = np.empty(10001, dtype=np.float64)
        self.conversion_ms.append((time.perf_counter() - started) * 1000)
        code = self.function(
            points,
            probabilities,
            offsets,
            sizes,
            len(options),
            remaining,
            earned,
            reference,
            ideal or 0.0,
            self.callback,
            output,
        )
        if code:
            raise ValueError(f"native prototype rejected inputs: {code}")
        output.flags.writeable = False
        return output


def benchmark(library: Path, repeats: int) -> dict:
    native = NativeProjection(library)
    catalog = load_scoring_catalog()
    current = catalog.profile("generic_dps_v1")
    profiles = (current, replace(current, roll_data_version=LEGACY_ROLL_VERSION))
    stats = tuple(
        ObservedStat(n, v, u, 0.99)
        for n, v, u in (
            ("暴擊", 7.5, "%"),
            ("攻擊", 10.1, "%"),
            ("共鳴效率", 10, "%"),
            ("暴擊傷害", 15, "%"),
            ("攻擊", 40, ""),
        )
    )
    results = []
    original = engine_module.score_distribution
    for profile in profiles:
        for opened in (1, 2, 3, 4):
            inputs = []

            def capture(*args, inputs=inputs):
                inputs.append(args)
                return original(*args)

            engine_module.score_distribution = capture
            try:
                expected = EchoScoringEngine().enumerate_remaining(
                    profile, stats[:opened], improvement_target=80
                )
            finally:
                engine_module.score_distribution = original
            args = inputs[0]
            numpy_timing, numpy_mass = measure(
                lambda args=args: score_distribution.__wrapped__(*args), repeats
            )
            native_timing, native_mass = measure(lambda args=args: native(*args), repeats)
            assert np.allclose(numpy_mass, native_mass, rtol=1e-12, atol=1e-14)
            engine_module.score_distribution = native
            try:
                actual = EchoScoringEngine().enumerate_remaining(
                    profile, stats[:opened], improvement_target=80
                )
            finally:
                engine_module.score_distribution = original
            assert actual.to_dict() == expected.to_dict()
            results.append(
                {
                    "version": profile.roll_data_version,
                    "level": opened * 5,
                    "numpy": numpy_timing,
                    "cpp_with_conversion": native_timing,
                }
            )
    # Decimal rounding boundaries must match Python, not C round()/nearbyint alone.
    for reference, ideal in ((2.1, None), (2.1, 3.0)):
        for index in range(10000):
            point = (index / 100 + 0.005) / 100 * reference
            args = ((((point, 1.0),),), 1, 0.0, reference, ideal)
            expected = score_distribution.__wrapped__(*args)
            assert np.array_equal(native(*args), expected), (reference, ideal, index)
    return {
        "results": results,
        "projection_parity_cases": 8,
        "rounding_boundary_cases": 20000,
        "conversion_median_ms": float(np.median(native.conversion_ms)),
        "library_bytes": library.stat().st_size,
        "scope": "uncached exact kernel including Python marshalling and tie callbacks",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    result = benchmark(args.library, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf8")
    print(f"Native comparison verified: {args.output}")


if __name__ == "__main__":
    main()
