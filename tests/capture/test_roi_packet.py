from __future__ import annotations

import numpy as np
import pytest

from wuwa_assistant.capture import (
    FrameLeaseAudit,
    FrameReleasedError,
    RoiExtractor,
    RoiPacket,
    RoiPacketKind,
)
from wuwa_assistant.capture.roi_packet import PROBE_ROIS


def reference_frame() -> np.ndarray:
    rows = np.arange(1440, dtype=np.uint16)[:, None]
    columns = np.arange(2560, dtype=np.uint16)[None, :]
    pixels = np.empty((1440, 2560, 3), dtype=np.uint8)
    pixels[:, :, 0] = columns % 251
    pixels[:, :, 1] = rows % 239
    pixels[:, :, 2] = (rows + columns) % 233
    return pixels


def test_extractor_returns_owned_read_only_allowlisted_rois_then_releases_frame() -> None:
    from wuwa_assistant.capture import CapturedFrame

    audit = FrameLeaseAudit()
    full_pixels = reference_frame()
    frame = CapturedFrame(full_pixels, "2026-08-01T00:00:00+00:00", 1, audit=audit)

    extracted = RoiExtractor().extract(frame, page_type="echo_enhancement")

    assert frame.is_released
    audit.assert_all_released()
    assert extracted.content is not None
    assert extracted.probe.kind is RoiPacketKind.PROBE
    assert extracted.content.kind is RoiPacketKind.CONTENT
    assert extracted.content.page_type == "echo_enhancement"
    assert {roi.name for roi in PROBE_ROIS} <= set(extracted.content.images)
    assert extracted.probe.total_bytes < full_pixels.nbytes
    assert extracted.content.total_bytes < full_pixels.nbytes
    assert "images=" not in repr(extracted.probe)

    for packet in (extracted.probe, extracted.content):
        for image in packet.images.values():
            assert image.flags.owndata
            assert image.base is None
            assert not image.flags.writeable
            assert not np.shares_memory(image, full_pixels)

    header = extracted.probe.image("page_header")
    expected = full_pixels[35:135, 45:330].copy()
    assert np.array_equal(header, expected)
    full_pixels.fill(0)
    assert np.array_equal(header, expected)
    with pytest.raises(ValueError, match="read-only"):
        header[0, 0, 0] = 255
    with pytest.raises(FrameReleasedError):
        _ = frame.pixels


def test_unknown_page_extracts_only_probe_and_still_releases() -> None:
    from wuwa_assistant.capture import CapturedFrame

    frame = CapturedFrame(reference_frame(), "now", 1)
    extracted = RoiExtractor().extract(frame, page_type="unknown")

    assert extracted.content is None
    assert set(extracted.probe.images) == {roi.name for roi in PROBE_ROIS}
    assert frame.is_released


def test_selection_equipment_and_set_crops_are_bounded_owned_same_frame_copies() -> None:
    from wuwa_assistant.capture import CapturedFrame
    from wuwa_assistant.vision.rois import SELECTION_EQUIPPED_SLOTS_ROI, SELECTION_SET_EFFECTS_ROI

    pixels = reference_frame()
    frame = CapturedFrame(pixels, "now", 1)
    extracted = RoiExtractor().extract(frame, page_type="echo_selection")
    assert frame.is_released
    assert extracted.content is not None
    assert extracted.content.total_bytes < pixels.nbytes
    for roi in (SELECTION_EQUIPPED_SLOTS_ROI, SELECTION_SET_EFFECTS_ROI):
        crop = extracted.content.image(roi.name)
        assert np.array_equal(crop, pixels[roi.slices])
        assert crop.flags.owndata
        assert not crop.flags.writeable
        assert not np.shares_memory(crop, pixels)


def test_roi_packet_rejects_non_allowlisted_or_wrong_sized_images() -> None:
    valid = {roi.name: np.zeros((roi.height, roi.width, 3), dtype=np.uint8) for roi in PROBE_ROIS}
    with pytest.raises(ValueError, match="allowlist"):
        RoiPacket(
            kind=RoiPacketKind.PROBE,
            captured_at="now",
            monotonic_timestamp_ns=1,
            monitor_index=1,
            sequence=1,
            images={**valid, "full_frame": np.zeros((1440, 2560, 3), dtype=np.uint8)},
        )

    valid["page_header"] = np.zeros((1440, 2560, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="page_header"):
        RoiPacket(
            kind=RoiPacketKind.PROBE,
            captured_at="now",
            monotonic_timestamp_ns=1,
            monitor_index=1,
            sequence=1,
            images=valid,
        )


def test_extractor_releases_frame_when_packet_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wuwa_assistant.capture import CapturedFrame
    from wuwa_assistant.capture import roi_packet as module

    audit = FrameLeaseAudit()
    frame = CapturedFrame(reference_frame(), "now", 1, audit=audit)

    def fail_packet(**_kwargs: object) -> None:
        raise RuntimeError("synthetic packet failure")

    monkeypatch.setattr(module, "RoiPacket", fail_packet)
    with pytest.raises(RuntimeError, match="synthetic packet failure"):
        module.RoiExtractor().extract(frame)

    assert frame.is_released
    audit.assert_all_released()
