"""Semantic ROI fingerprints and a consecutive-frame stability gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from .preprocess import Image, crop_roi, preprocess_for_hash, to_grayscale
from .rois import Roi


def perceptual_hash(image: Image, *, hash_size: int = 8, high_frequency_factor: int = 4) -> int:
    """Return a DCT pHash encoded as a non-negative Python integer.

    The DC coefficient is ignored so uniform brightness changes do not look like semantic
    UI changes.  The default result contains 64 bits.
    """

    if hash_size < 2:
        raise ValueError("hash_size must be at least 2")
    if high_frequency_factor < 1:
        raise ValueError("high_frequency_factor must be positive")

    gray = to_grayscale(image)
    side = hash_size * high_frequency_factor
    resized = cv2.resize(gray, (side, side), interpolation=cv2.INTER_AREA)
    resized_float = resized.astype(np.float32)
    dct = cv2.dct(resized_float)
    low_frequency = dct[:hash_size, :hash_size]
    coefficients = low_frequency.ravel()
    median = float(np.median(coefficients[1:]))
    bits = coefficients > median
    bits[0] = False

    value = 0
    for enabled in bits:
        value = (value << 1) | int(enabled)
    return value


def hamming_distance(left: int, right: int) -> int:
    """Return the number of differing bits between two integer hashes."""

    if left < 0 or right < 0:
        raise ValueError("hashes must be non-negative integers")
    return (left ^ right).bit_count()


@dataclass(frozen=True, slots=True)
class SemanticFingerprint:
    """One pHash per semantic ROI, preserving which field changed."""

    roi_names: tuple[str, ...]
    hashes: tuple[int, ...]
    hash_size: int = 8

    def __post_init__(self) -> None:
        if not self.roi_names:
            raise ValueError("a semantic fingerprint needs at least one ROI")
        if len(self.roi_names) != len(self.hashes):
            raise ValueError("roi_names and hashes must have the same length")
        if len(set(self.roi_names)) != len(self.roi_names):
            raise ValueError("semantic fingerprint ROI names must be unique")
        if self.hash_size < 2:
            raise ValueError("hash_size must be at least 2")
        if any(value < 0 for value in self.hashes):
            raise ValueError("fingerprint hashes must be non-negative")

    def roi_distances(self, other: SemanticFingerprint) -> tuple[int, ...]:
        self._require_compatible(other)
        return tuple(
            hamming_distance(left, right)
            for left, right in zip(self.hashes, other.hashes, strict=True)
        )

    def max_distance(self, other: SemanticFingerprint) -> int:
        return max(self.roi_distances(other))

    def total_distance(self, other: SemanticFingerprint) -> int:
        return sum(self.roi_distances(other))

    def to_hex(self) -> str:
        digits = (self.hash_size * self.hash_size + 3) // 4
        return ":".join(f"{value:0{digits}x}" for value in self.hashes)

    def _require_compatible(self, other: SemanticFingerprint) -> None:
        if self.hash_size != other.hash_size or self.roi_names != other.roi_names:
            raise ValueError("semantic fingerprints use different ROI layouts")


def _normalise_rois(rois: Mapping[str, Roi] | Sequence[Roi]) -> tuple[tuple[str, Roi], ...]:
    if isinstance(rois, Mapping):
        items = tuple(rois.items())
        if any(name != roi.name for name, roi in items):
            raise ValueError("ROI mapping keys must match Roi.name")
    else:
        items = tuple((roi.name, roi) for roi in rois)
    if not items:
        raise ValueError("at least one ROI is required")
    names = tuple(name for name, _roi in items)
    if len(set(names)) != len(names):
        raise ValueError("ROI names must be unique")
    return items


def semantic_roi_fingerprint(
    frame: Image,
    rois: Mapping[str, Roi] | Sequence[Roi],
    *,
    hash_size: int = 8,
    foreground_mask: bool = True,
    require_reference_size: bool = True,
) -> SemanticFingerprint:
    """Hash only named UI fields, never the character model or full animated background."""

    items = _normalise_rois(rois)
    hashes: list[int] = []
    for _name, roi in items:
        cropped = crop_roi(
            frame,
            roi,
            require_reference_size=require_reference_size,
            copy=False,
        )
        prepared = preprocess_for_hash(cropped) if foreground_mask else cropped
        hashes.append(perceptual_hash(prepared, hash_size=hash_size))
    return SemanticFingerprint(
        roi_names=tuple(name for name, _roi in items),
        hashes=tuple(hashes),
        hash_size=hash_size,
    )


# Short alias for call sites that already know the fingerprint is ROI-based.
semantic_fingerprint = semantic_roi_fingerprint


def fingerprints_match(
    left: SemanticFingerprint,
    right: SemanticFingerprint,
    *,
    max_hamming_distance: int,
) -> bool:
    """Require every semantic field to remain within the Hamming threshold."""

    if max_hamming_distance < 0:
        raise ValueError("max_hamming_distance may not be negative")
    if left.hash_size != right.hash_size or left.roi_names != right.roi_names:
        return False
    return left.max_distance(right) <= max_hamming_distance


@dataclass(frozen=True, slots=True)
class StabilityDecision:
    """Result of observing one frame or precomputed fingerprint."""

    is_stable: bool
    became_stable: bool
    should_process: bool
    consecutive_frames: int
    candidate_distance: int | None
    fingerprint: SemanticFingerprint


class StabilityGate:
    """Wait for equivalent consecutive fingerprints and suppress duplicate stable states."""

    def __init__(
        self,
        *,
        required_consecutive: int = 3,
        max_hamming_distance: int = 4,
        duplicate_hamming_distance: int | None = None,
    ) -> None:
        if required_consecutive < 1:
            raise ValueError("required_consecutive must be at least 1")
        if max_hamming_distance < 0:
            raise ValueError("max_hamming_distance may not be negative")
        if duplicate_hamming_distance is not None and duplicate_hamming_distance < 0:
            raise ValueError("duplicate_hamming_distance may not be negative")
        self.required_consecutive = required_consecutive
        self.max_hamming_distance = max_hamming_distance
        self.duplicate_hamming_distance = (
            max_hamming_distance
            if duplicate_hamming_distance is None
            else duplicate_hamming_distance
        )
        self.reset(clear_last_processed=True)

    @property
    def consecutive_frames(self) -> int:
        return self._consecutive_frames

    @property
    def last_processed(self) -> SemanticFingerprint | None:
        return self._last_processed

    def reset(self, *, clear_last_processed: bool = False) -> None:
        """Reset an in-flight candidate; optionally also clear duplicate suppression."""

        self._candidate: SemanticFingerprint | None = None
        self._consecutive_frames = 0
        self._candidate_is_stable = False
        if clear_last_processed or not hasattr(self, "_last_processed"):
            self._last_processed: SemanticFingerprint | None = None

    def update(self, fingerprint: SemanticFingerprint) -> StabilityDecision:
        """Observe one fingerprint and return whether OCR/state processing should run."""

        distance: int | None = None
        if self._candidate is None:
            self._candidate = fingerprint
            self._consecutive_frames = 1
            self._candidate_is_stable = False
        else:
            same_layout = (
                self._candidate.hash_size == fingerprint.hash_size
                and self._candidate.roi_names == fingerprint.roi_names
            )
            distance = self._candidate.max_distance(fingerprint) if same_layout else None
            if distance is not None and distance <= self.max_hamming_distance:
                self._consecutive_frames += 1
            else:
                self._candidate = fingerprint
                self._consecutive_frames = 1
                self._candidate_is_stable = False

        is_stable = self._consecutive_frames >= self.required_consecutive
        became_stable = is_stable and not self._candidate_is_stable
        should_process = False
        if became_stable:
            is_duplicate = self._last_processed is not None and fingerprints_match(
                self._last_processed,
                self._candidate,
                max_hamming_distance=self.duplicate_hamming_distance,
            )
            should_process = not is_duplicate
            if should_process:
                self._last_processed = self._candidate

        self._candidate_is_stable = is_stable
        return StabilityDecision(
            is_stable=is_stable,
            became_stable=became_stable,
            should_process=should_process,
            consecutive_frames=self._consecutive_frames,
            candidate_distance=distance,
            fingerprint=fingerprint,
        )

    def observe(
        self,
        frame: Image,
        rois: Mapping[str, Roi] | Sequence[Roi],
        *,
        hash_size: int = 8,
        foreground_mask: bool = True,
        require_reference_size: bool = True,
    ) -> StabilityDecision:
        """Fingerprint a raw in-memory frame and feed it to the gate."""

        fingerprint = semantic_roi_fingerprint(
            frame,
            rois,
            hash_size=hash_size,
            foreground_mask=foreground_mask,
            require_reference_size=require_reference_size,
        )
        return self.update(fingerprint)
