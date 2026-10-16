from __future__ import annotations

from wuwa_assistant.evaluation import compare_expected_subset


def test_expected_comparison_uses_only_supplied_fields_and_reports_confidence() -> None:
    actual = {
        "page_type": "echo_enhancement",
        "name": "不熄獵手",
        "level": 15,
        "confidence": {"page_type": 0.99, "name": 0.98, "level": 0.97},
        "extra_debug_field": "allowed",
    }
    expected = {
        "page_type": "echo_enhancement",
        "name": "不熄獵手",
        "level": 10,
    }

    differences, metrics = compare_expected_subset("synthetic.png", actual, expected)

    assert [(item.field, item.expected, item.actual) for item in differences] == [("level", 10, 15)]
    assert metrics["page_type"].as_dict()["accuracy"] == 1.0
    assert metrics["name"].as_dict()["mean_confidence"] == 0.98
    assert metrics["level"].as_dict()["accuracy"] == 0.0
