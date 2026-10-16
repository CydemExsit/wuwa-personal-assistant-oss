"""Core data models.

The models deliberately keep OCR uncertainty visible. A missing or rejected field is
represented by ``None``; downstream code must never fill it from game-specific guesses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class PageType(StrEnum):
    CHARACTER_ECHO_OVERVIEW = "character_echo_overview"
    ECHO_SELECTION = "echo_selection"
    ECHO_ENHANCEMENT = "echo_enhancement"
    ENHANCEMENT_MATERIAL_SELECTION = "enhancement_material_selection"
    UNKNOWN = "unknown"


class StatKind(StrEnum):
    PRIMARY = "primary"
    FIXED = "fixed"
    SUBSTAT = "substat"


class SessionState(StrEnum):
    ACTIVE = "active"
    DISCARDED_ACTIVE = "discarded_active"
    PENDING_CONSUMPTION = "pending_consumption"
    CONSUMED_ENDED = "consumed_ended"


class EventType(StrEnum):
    PAGE_CHANGED = "page_changed"
    ECHO_SELECTED = "echo_selected"
    ECHO_SESSION_STARTED = "echo_session_started"
    ECHO_UPGRADED = "echo_upgraded"
    ECHO_MARKED_DISCARDED = "echo_marked_discarded"
    ECHO_USED_AS_MATERIAL = "echo_used_as_material"
    ECHO_SESSION_ENDED = "echo_session_ended"
    NEW_ECHO_SESSION_STARTED = "new_echo_session_started"


@dataclass(slots=True)
class OcrToken:
    """One OCR line with an axis-aligned, ROI-relative bounding box."""

    text: str
    confidence: float
    bbox: tuple[int, int, int, int]


@dataclass(slots=True)
class EchoStat:
    stat_kind: StatKind
    name: str | None
    value: int | float | None
    unit: str | None
    confidence: float | None
    name_confidence: float | None
    value_confidence: float | None
    raw_name: str | None = None
    raw_value: str | None = None
    slot_index: int | None = None

    @property
    def semantic_key(self) -> tuple[str | None, int | float | None, str | None]:
        return (self.name, self.value, self.unit)


@dataclass(slots=True)
class PageClassification:
    page_type: PageType
    confidence: float
    raw_ocr: dict[str, list[OcrToken]] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    debug_roi_paths: list[str] = field(default_factory=list)
    debug_ocr_path: str | None = None


@dataclass(frozen=True, slots=True)
class SelectionIdentityEvidence:
    """Pixel-free evidence used only to reconnect a revisit within one capture."""

    version: str
    selected_cell: int
    selected_card_bbox: tuple[int, int, int, int]
    card_art_phash: str
    context_phashes: tuple[str, ...]


@dataclass(slots=True)
class EchoObservation:
    page_type: PageType
    name: str | None = None
    set_name: str | None = None
    level: int | None = None
    projected_level: int | None = None
    cost: int | None = None
    main_stats: list[EchoStat] = field(default_factory=list)
    projected_main_stats: list[EchoStat] = field(default_factory=list)
    sub_stats: list[EchoStat] = field(default_factory=list)
    locked_substat_slots: int | None = None
    confidence: dict[str, float | None] = field(default_factory=dict)
    source_timestamp: str | None = None
    source_file: str | None = None
    is_discarded: bool | None = None
    is_equipped: bool | None = None
    equipped_character_name: str | None = None
    equipped_slot_index: int | None = None
    material_source_selected: bool | None = None
    material_source_level: int | None = None
    raw_ocr: dict[str, list[OcrToken]] = field(default_factory=dict)
    debug_roi_paths: list[str] = field(default_factory=list)
    debug_ocr_path: str | None = None
    semantic_fingerprint: str | None = None
    selection_identity: SelectionIdentityEvidence | None = None
    revisit_status: str | None = None
    revisit_session_id: str | None = None

    @property
    def overall_confidence(self) -> float | None:
        values = [value for value in self.confidence.values() if value is not None]
        return min(values) if values else None

    def semantic_key(self) -> tuple[Any, ...]:
        """Return state content only; excludes time, file paths, and OCR confidence."""

        return (
            self.page_type,
            self.name,
            self.set_name,
            self.level,
            self.projected_level,
            self.cost,
            tuple(stat.semantic_key for stat in self.main_stats),
            tuple(stat.semantic_key for stat in self.projected_main_stats),
            tuple(stat.semantic_key for stat in self.sub_stats),
            self.locked_substat_slots,
            self.is_discarded,
            self.is_equipped,
            self.equipped_character_name,
            self.equipped_slot_index,
            self.material_source_selected,
            self.material_source_level,
        )


@dataclass(slots=True)
class EchoSession:
    local_session_id: str
    observations: list[EchoObservation] = field(default_factory=list)
    current_state: SessionState = SessionState.ACTIVE
    is_discarded: bool = False
    was_consumed: bool = False
    consumed_into_session_id: str | None = None

    @property
    def latest_observation(self) -> EchoObservation | None:
        return self.observations[-1] if self.observations else None


@dataclass(slots=True)
class EchoEvent:
    event_type: EventType
    session_id: str | None
    previous_observation: EchoObservation | None = None
    current_observation: EchoObservation | None = None
    confidence: float | None = None
    notes: list[str] = field(default_factory=list)
    related_session_id: str | None = None
    previous_level: int | None = None
    current_level: int | None = None
    new_substats: list[EchoStat] = field(default_factory=list)
    source_file: str | None = None

    def dedupe_key(self) -> tuple[Any, ...]:
        return (
            self.event_type,
            self.session_id,
            self.related_session_id,
            self.previous_level,
            self.current_level,
            tuple(stat.semantic_key for stat in self.new_substats),
            tuple(self.notes),
        )


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, enums, and paths to JSON-safe values."""

    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value
