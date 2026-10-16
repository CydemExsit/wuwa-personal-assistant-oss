from __future__ import annotations

import numpy as np
import pytest

from wuwa_assistant.capture import CapturedFrame, FrameLeaseAudit, RoiExtractor
from wuwa_assistant.capture.roi_packet import ALLOWED_ROIS
from wuwa_assistant.display import RESOLUTIONS


@pytest.mark.parametrize("resolution", RESOLUTIONS.values())
def test_every_allowlisted_roi_maps_native_pixels_and_releases_frame(resolution) -> None:
    width, height = resolution
    # Independent horizontal/vertical ramps identify which native pixels were
    # sampled, including offsets and right/bottom edges, without OCR mocks.
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[:, :, 0] = np.linspace(0, 240, width, dtype=np.uint8)[None, :]
    pixels[:, :, 1] = np.linspace(0, 240, height, dtype=np.uint8)[:, None]
    audit = FrameLeaseAudit()
    with CapturedFrame.now(pixels, 1, audit=audit) as frame:
        for roi in ALLOWED_ROIS.values():
            crop = frame.copy_roi(roi)
            assert crop.shape == (roi.height, roi.width, 3)
            assert not np.shares_memory(crop, pixels)
            expected_x = (roi.x1 + roi.x2) / 2 / 2560 * 240
            expected_y = (roi.y1 + roi.y2) / 2 / 1440 * 240
            center = crop[roi.height // 2, roi.width // 2]
            assert float(center[0]) == pytest.approx(expected_x, abs=2)
            assert float(center[1]) == pytest.approx(expected_y, abs=2)
    audit.assert_all_released()


def test_1080p_packet_has_owned_readonly_reference_crops_and_no_frame() -> None:
    audit = FrameLeaseAudit()
    frame = CapturedFrame.now(np.full((1080, 1920, 3), 42, dtype=np.uint8), 1, audit=audit)
    result = RoiExtractor().extract(frame, page_type="echo_selection")
    assert frame.is_released
    assert result.content is not None
    for image in result.content.images.values():
        assert not image.flags.writeable
        assert np.all(image == 42)
    audit.assert_all_released()
