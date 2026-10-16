"""Subset-based comparison against optional, local ground truth JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Difference:
    source_file: str
    field: str
    expected: Any
    actual: Any


@dataclass(slots=True)
class FieldMetric:
    correct: int = 0
    total: int = 0
    confidences: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "correct": self.correct,
            "total": self.total,
            "accuracy": round(self.correct / self.total, 4) if self.total else None,
            "mean_confidence": (
                round(sum(self.confidences) / len(self.confidences), 4)
                if self.confidences
                else None
            ),
        }


def _metric_name(path: str) -> str:
    parts = path.replace("]", "").replace("[", ".").split(".")
    normalized = [part for part in parts if part and not part.isdigit()]
    return ".".join(normalized)


def _confidence_for_path(actual_root: dict[str, Any], path: str) -> float | None:
    if "." not in path and "[" not in path:
        value = actual_root.get("confidence", {}).get(path)
        return float(value) if isinstance(value, (int, float)) else None

    parts = path.replace("]", "").replace("[", ".").split(".")
    if len(parts) >= 3 and parts[0] in {"main_stats", "sub_stats", "projected_main_stats"}:
        try:
            stat = actual_root[parts[0]][int(parts[1])]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        field_name = parts[2]
        confidence_key = "name_confidence" if field_name == "name" else "value_confidence"
        value = stat.get(confidence_key, stat.get("confidence"))
        return float(value) if isinstance(value, (int, float)) else None
    return None


def compare_expected_subset(
    source_file: str,
    actual: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[list[Difference], dict[str, FieldMetric]]:
    """Compare only keys supplied by expected data; extra actual diagnostics are allowed."""

    differences: list[Difference] = []
    metrics: dict[str, FieldMetric] = {}

    def visit(actual_value: Any, expected_value: Any, path: str) -> None:
        if isinstance(expected_value, dict):
            actual_dict = actual_value if isinstance(actual_value, dict) else {}
            for key, child in expected_value.items():
                visit(actual_dict.get(key), child, f"{path}.{key}" if path else key)
            return
        if isinstance(expected_value, list):
            actual_list = actual_value if isinstance(actual_value, list) else []
            if len(actual_list) != len(expected_value):
                differences.append(
                    Difference(source_file, f"{path}.length", len(expected_value), len(actual_list))
                )
            for index, child in enumerate(expected_value):
                actual_child = actual_list[index] if index < len(actual_list) else None
                visit(actual_child, child, f"{path}[{index}]")
            return

        metric = metrics.setdefault(_metric_name(path), FieldMetric())
        metric.total += 1
        confidence = _confidence_for_path(actual, path)
        if confidence is not None:
            metric.confidences.append(confidence)
        if actual_value == expected_value:
            metric.correct += 1
        else:
            differences.append(Difference(source_file, path, expected_value, actual_value))

    visit(actual, expected, "")
    return differences, metrics


def merge_metrics(target: dict[str, FieldMetric], source: dict[str, FieldMetric]) -> None:
    for name, metric in source.items():
        current = target.setdefault(name, FieldMetric())
        current.correct += metric.correct
        current.total += metric.total
        current.confidences.extend(metric.confidences)
