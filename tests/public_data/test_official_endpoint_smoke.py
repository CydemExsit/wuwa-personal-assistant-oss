from __future__ import annotations

import os
from pathlib import Path

import pytest

from wuwa_assistant.public_data.service import OfficialPublicDataService


@pytest.mark.skipif(
    os.environ.get("WUWA_OFFICIAL_DATA_SMOKE") != "1",
    reason="explicit opt-in network smoke only",
)
def test_real_official_endpoint_contract(tmp_path: Path) -> None:
    result = OfficialPublicDataService(tmp_path).synchronize()
    assert result.feed is not None
    assert result.feed.notices
    assert all("kurogames.com" in item.official_url for item in result.feed.notices)
