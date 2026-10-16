"""Immutable, array-free contracts for the live recognition runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from wuwa_assistant.capture.roi_packet import RoiPacketKind

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class LiveStatus(StrEnum):
    INACTIVE = "inactive"
    WAITING_FOR_PAGE = "waiting_for_page"
    WAITING_FOR_REQUEST = "waiting_for_request"
    TRANSITIONING = "transitioning"
    STABLE = "stable"
    RECOGNIZING = "recognizing"
    LOW_CONFIDENCE = "low_confidence"
    STOPPED = "stopped"
    ERROR = "error"


class PacketDisposition(StrEnum):
    OBSERVING = "observing"
    QUEUED = "queued"
    REPLACED_PENDING = "replaced_pending"
    DROPPED_STOPPED = "dropped_stopped"
    DROPPED_STALE_GENERATION = "dropped_stale_generation"
    DROPPED_OUT_OF_ORDER = "dropped_out_of_order"
    DROPPED_DUPLICATE = "dropped_duplicate"


@dataclass(frozen=True, slots=True)
class GenerationToken:
    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError("generation token must be positive")


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """JSON-safe result returned by an injected ROI analyzer."""

    page_type: str
    relevant: bool
    confidence: float | None
    observation: Mapping[str, object]
    request_content: bool = False
    unchanged: bool = False
    requires_commit: bool = False

    def __post_init__(self) -> None:
        if not self.page_type.strip():
            raise ValueError("page_type cannot be empty")
        if self.confidence is not None and (
            not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be null or within [0, 1]")
        normalized = _copy_json_object(self.observation)
        object.__setattr__(self, "observation", normalized)


@dataclass(frozen=True, slots=True)
class LiveStatusUpdate:
    status: LiveStatus
    generation: int | None
    emitted_at: float
    reason: str
    source_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class LiveRecognitionResult:
    """Callback DTO that deliberately contains no ROI pixels or frame object."""

    generation: int
    source_sequence: int
    captured_at: str
    completed_at: float
    packet_kind: RoiPacketKind
    state_fingerprint: str
    analysis: AnalysisResult


@dataclass(frozen=True, slots=True)
class LiveRuntimeError:
    generation: int
    source_sequence: int | None
    emitted_at: float
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class CoordinatorStats:
    submitted_packets: int
    stale_generation_dropped: int
    stopped_dropped: int
    out_of_order_dropped: int
    unstable_samples: int
    duplicate_states_dropped: int
    scheduled: int
    pending_replaced: int
    analysis_started: int
    analysis_completed: int
    analysis_errors: int
    canceled_results_dropped: int
    superseded_results_dropped: int
    results_emitted: int
    callback_errors: int


def _copy_json_object(value: Mapping[str, object]) -> dict[str, JsonValue]:
    copied: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("analysis observation keys must be strings")
        copied[key] = _copy_json_value(item, path=f"observation.{key}")
    return copied


def _copy_json_value(value: Any, *, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} must not contain a non-finite float")
        return value
    if isinstance(value, Mapping):
        nested: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            nested[key] = _copy_json_value(item, path=f"{path}.{key}")
        return nested
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")
