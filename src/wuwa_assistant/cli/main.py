"""CLI for single-image and ordered-static-sequence analysis."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from wuwa_assistant.evaluation import (
    Difference,
    FieldMetric,
    compare_expected_subset,
    merge_metrics,
)
from wuwa_assistant.models import EchoObservation, to_jsonable
from wuwa_assistant.ocr.rapidocr_engine import RapidOcrEngine
from wuwa_assistant.pipeline import ImageAnalyzer, StaticSequenceAnalyzer

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def _natural_key(path: Path) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)
    )


def collect_image_paths(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        candidate = Path(item).expanduser()
        if candidate.is_dir():
            paths.extend(
                path
                for path in candidate.iterdir()
                if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
            )
        elif candidate.is_file() and candidate.suffix.casefold() in IMAGE_EXTENSIONS:
            paths.append(candidate)
        else:
            raise FileNotFoundError(f"Image path or directory not found: {candidate}")
    unique = {path.resolve(): path.resolve() for path in paths}
    return sorted(unique.values(), key=_natural_key)


def _load_expected(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), dict):
        raise ValueError("Expected JSON must contain an 'images' object keyed by filename")
    return payload


def _auto_expected(inputs: list[str], explicit: str | None) -> Path | None:
    if explicit is not None:
        return Path(explicit).resolve()
    if len(inputs) == 1:
        candidate = Path(inputs[0])
        if candidate.is_dir() and (candidate / "expected.json").is_file():
            return (candidate / "expected.json").resolve()
    return None


def _evaluate_observations(
    observations: list[EchoObservation],
    expected_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int | float | None]]]:
    if expected_payload is None:
        return [], {}
    expected_images = expected_payload["images"]
    differences: list[Difference] = []
    metrics: dict[str, FieldMetric] = {}
    for observation in observations:
        source_name = Path(observation.source_file or "").name
        expected = expected_images.get(source_name)
        if not isinstance(expected, dict):
            continue
        actual = to_jsonable(observation)
        image_differences, image_metrics = compare_expected_subset(source_name, actual, expected)
        differences.extend(image_differences)
        merge_metrics(metrics, image_metrics)
    return (
        [to_jsonable(item) for item in differences],
        {name: metric.as_dict() for name, metric in sorted(metrics.items())},
    )


def _write_or_print(payload: dict[str, Any], output_path: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path is None:
        print(rendered)
        return
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps({"output_path": str(target)}, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m wuwa_assistant",
        description="Offline 2560x1440 Traditional Chinese echo screenshot analyzer",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("analyze-image", help="analyze one local image")
    single.add_argument("image_path")
    single.add_argument("--expected", help="optional local expected-answer JSON")
    single.add_argument("--debug-dir", default="debug_output")
    single.add_argument("--output", help="write JSON to this path instead of stdout")

    sequence = subparsers.add_parser(
        "analyze-sequence", help="analyze one directory or an ordered list of image files"
    )
    sequence.add_argument("inputs", nargs="+")
    sequence.add_argument("--expected", help="optional local expected-answer JSON")
    sequence.add_argument("--debug-dir", default="debug_output")
    sequence.add_argument("--output", help="write JSON to this path instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        debug_root = Path(args.debug_dir).resolve()
        analyzer = ImageAnalyzer(RapidOcrEngine(), debug_root=debug_root)
        if args.command == "analyze-image":
            observation = analyzer.analyze_path(Path(args.image_path))
            expected_path = Path(args.expected).resolve() if args.expected else None
            expected = _load_expected(expected_path)
            differences, metrics = _evaluate_observations([observation], expected)
            payload = {
                "mode": "offline_static_image",
                "observation": to_jsonable(observation),
                "differences": differences,
                "field_metrics": metrics,
            }
            _write_or_print(payload, args.output)
            return 0

        paths = collect_image_paths(args.inputs)
        if not paths:
            raise ValueError("No supported image files were found")
        expected_path = _auto_expected(args.inputs, args.expected)
        expected = _load_expected(expected_path)
        analysis = StaticSequenceAnalyzer(analyzer).analyze_paths(paths)
        differences, metrics = _evaluate_observations(analysis.observations, expected)
        payload = {
            "mode": "ordered_user_provided_stable_snapshots",
            "sample_count": len(analysis.observations),
            "expected_file": str(expected_path) if expected_path else None,
            "results": [
                {
                    "source_file": observation.source_file,
                    "page_type": observation.page_type,
                    "observation": to_jsonable(observation),
                    "debug_roi_paths": observation.debug_roi_paths,
                    "debug_ocr_path": observation.debug_ocr_path,
                }
                for observation in analysis.observations
            ],
            "events": to_jsonable(analysis.events),
            "sessions": to_jsonable(analysis.sessions),
            "differences": differences,
            "field_metrics": metrics,
        }
        _write_or_print(payload, args.output)
        return 0
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
