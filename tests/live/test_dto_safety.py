from __future__ import annotations

import numpy as np
import pytest

from wuwa_assistant.capture import RoiPacket as CaptureRoiPacket
from wuwa_assistant.live import AnalysisResult, RoiPacket


def test_live_runtime_reuses_the_shared_capture_roi_packet() -> None:
    assert RoiPacket is CaptureRoiPacket


def test_analysis_result_rejects_array_payloads_and_copies_json_data() -> None:
    with pytest.raises(TypeError, match="unsupported value type ndarray"):
        AnalysisResult("echo_enhancement", True, 0.9, {"frame": np.zeros((2, 2))})

    source: dict[str, object] = {"name": "不熄獵手", "stats": ["暴擊"]}
    result = AnalysisResult("echo_enhancement", True, 0.9, source)
    source["name"] = "changed"
    assert result.observation["name"] == "不熄獵手"
