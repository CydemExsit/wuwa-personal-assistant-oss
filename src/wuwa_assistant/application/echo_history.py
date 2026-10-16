"""Transactional persistence for live echo recognition and manual corrections.

The live capture pipeline hands this service JSON mappings only.  Pixel arrays,
frames, screenshots, and ROI images are rejected before a transaction begins.
Every public operation uses a fresh, short SQLAlchemy session so the service can
be called from a worker without sharing ORM objects with the Qt thread.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from wuwa_assistant.models import (
    EventType,
    PageType,
    SelectionIdentityEvidence,
    SessionState,
    StatKind,
)
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    DataSetRecord,
    EchoEventRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    ObservationCorrectionRecord,
    ScoringProfileRecord,
    utc_now,
)
from wuwa_assistant.vision.selection_identity import visual_identity_matches

from .characters import assign_echo_record
from .echo_duplicates import group_echo_records
from .observation_review import confirmed_fields, observation_review_issues
from .recognized_equipment import clear_recognized_equipment, register_recognized_equipment

_IMAGE_PAYLOAD_KEYS = frozenset(
    {
        "frame",
        "frames",
        "full_frame",
        "full_frames",
        "image",
        "images",
        "pixels",
        "pixel_data",
        "roi_image",
        "roi_images",
        "roi_pixels",
        "screenshot",
        "screenshots",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "page_type",
        "name",
        "set_name",
        "level",
        "projected_level",
        "cost",
        "main_stats",
        "projected_main_stats",
        "sub_stats",
        "locked_substat_slots",
        "confidence",
        "source_timestamp",
        "source_file",
        "is_discarded",
        "is_equipped",
        "equipped_character_name",
        "equipped_slot_index",
        "material_source_selected",
        "material_source_level",
        "raw_ocr",
        "debug_roi_paths",
        "debug_ocr_path",
        "semantic_fingerprint",
        "selection_identity",
        "revisit_status",
        "revisit_session_id",
    }
)
_CORRECTABLE_SCALARS = frozenset(
    {
        "name",
        "set_name",
        "level",
        "projected_level",
        "cost",
        "locked_substat_slots",
        "is_discarded",
        "is_equipped",
        "equipped_character_name",
        "equipped_slot_index",
        "material_source_selected",
        "material_source_level",
    }
)
_STAT_COLLECTIONS = frozenset({"main_stats", "projected_main_stats", "sub_stats"})
_STAT_FIELDS = frozenset(
    {
        "stat_kind",
        "name",
        "value",
        "unit",
        "confidence",
        "name_confidence",
        "value_confidence",
        "raw_name",
        "raw_value",
        "slot_index",
    }
)
_CAPTURE_STOP_STATUSES = frozenset({"stopped", "error", "cancelled", "interrupted"})
_ECHO_LEVEL_STAGES = frozenset(range(26))


class EchoDisposition(StrEnum):
    """User-confirmed outcome; separate from the reversible in-game discard mark."""

    UNDECIDED = "undecided"
    KEPT = "kept"
    DISCARDED = "discarded"
    EQUIPPED = "equipped"


@dataclass(frozen=True, slots=True)
class CaptureSessionSnapshot:
    id: str
    data_set_id: str
    backend_kind: str
    monitor_index: int
    sampling_hz: float
    stable_wait_ms: int
    status: str
    started_at: str
    stopped_at: str | None
    assigned_character_id: str | None
    scoring_profile_id: str | None
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    capture_session_id: str
    echo_session_id: str | None
    observation_id: str | None
    event_ids: tuple[str, ...]
    duplicate: bool
    created_character_id: str | None = None
    equipment_changed: bool = False


@dataclass(frozen=True, slots=True)
class ObservationSnapshot:
    id: str
    sequence_no: int
    observed_at: str
    requires_review: bool
    raw_payload: dict[str, Any]
    effective_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EchoEventSnapshot:
    id: str
    sequence_no: int
    event_type: str
    echo_session_id: str | None
    related_session_id: str | None
    confidence: float | None
    notes: tuple[str, ...]
    payload: dict[str, Any]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class CorrectionSnapshot:
    id: str
    observation_id: str
    field_path: str
    old_value: Any
    new_value: Any
    reason: str | None
    actor: str
    corrected_at: str


@dataclass(frozen=True, slots=True)
class EchoSessionSummary:
    id: str
    capture_session_id: str
    local_session_id: str
    current_state: str
    is_discarded: bool
    was_consumed: bool
    consumed_into_session_id: str | None
    started_at: str
    ended_at: str | None
    echo_id: str | None
    name: str | None
    level: int | None
    cost: int | None
    final_status: str
    score: float | None
    character_id: str | None
    slot_index: int | None
    observation_count: int
    latest_observation_id: str | None
    requires_review: bool
    rescan_count: int = 0
    grouped_session_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoHistory:
    session: EchoSessionSummary
    capture: CaptureSessionSnapshot
    observations: tuple[ObservationSnapshot, ...]
    events: tuple[EchoEventSnapshot, ...]
    corrections: tuple[CorrectionSnapshot, ...]


class EchoHistoryService:
    """Restart-safe application boundary for structured recognition history."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def ensure_active_primary_dataset(self, *, name: str = "主要資料") -> str:
        """Return one active primary partition, creating it on first use."""

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("data set name cannot be empty")
        if len(normalized_name) > 120:
            raise ValueError("data set name must not exceed 120 characters")

        with self._session_factory.begin() as session:
            records = list(
                session.scalars(
                    select(DataSetRecord)
                    .where(DataSetRecord.kind == "primary")
                    .order_by(DataSetRecord.is_active.desc(), DataSetRecord.created_at)
                ).all()
            )
            selected = next((item for item in records if item.is_active), None)
            if selected is None:
                selected = next((item for item in records if item.name == normalized_name), None)
            if selected is None and records:
                selected = records[0]
            if selected is None:
                selected = DataSetRecord(
                    name=normalized_name,
                    kind="primary",
                    is_active=True,
                )
                session.add(selected)
                session.flush()
                return selected.id

            for record in records:
                record.is_active = record.id == selected.id
            session.flush()
            return selected.id

    def recover_interrupted_capture_sessions(
        self,
        *,
        data_set_id: str | None = None,
        stopped_at: datetime | None = None,
    ) -> int:
        """Close sessions left active by a prior crash without deleting their history."""

        when = _as_utc(stopped_at or utc_now())
        with self._session_factory.begin() as session:
            statement = select(CaptureSessionRecord).where(CaptureSessionRecord.status == "active")
            if data_set_id is not None:
                statement = statement.where(CaptureSessionRecord.data_set_id == data_set_id)
            records = list(session.scalars(statement).all())
            for record in records:
                record.status = "interrupted"
                record.stopped_at = when
                if not record.error_summary:
                    record.error_summary = "previous application session ended unexpectedly"
            return len(records)

    def start_capture(
        self,
        *,
        backend_kind: str,
        monitor_index: int,
        sampling_hz: float,
        stable_wait_ms: int,
        app_version: str,
        data_set_id: str | None = None,
        assigned_character_id: str | None = None,
        scoring_profile_id: str | None = None,
        roi_config_version: str | None = None,
        metadata: Mapping[str, object] | None = None,
        started_at: datetime | None = None,
    ) -> CaptureSessionSnapshot:
        """Recover stale sessions and begin a new explicitly user-started capture."""

        normalized_backend = _required_text(backend_kind, "backend_kind", maximum=60)
        normalized_version = _required_text(app_version, "app_version", maximum=40)
        if monitor_index < 0:
            raise ValueError("monitor_index must be nonnegative")
        if not isfinite(sampling_hz) or not 2.0 <= sampling_hz <= 8.0:
            raise ValueError("sampling_hz must be within [2, 8]")
        if stable_wait_ms <= 0:
            raise ValueError("stable_wait_ms must be positive")
        safe_metadata = _json_object(metadata or {}, label="capture metadata")
        when = _as_utc(started_at or utc_now())
        selected_data_set_id = data_set_id or self.ensure_active_primary_dataset()

        with self._session_factory.begin() as session:
            data_set = session.get(DataSetRecord, selected_data_set_id)
            if data_set is None:
                raise LookupError(f"data set not found: {selected_data_set_id}")
            character = _get_optional_fk(
                session,
                CharacterRecord,
                assigned_character_id,
                "character",
            )
            profile = _get_optional_fk(
                session,
                ScoringProfileRecord,
                scoring_profile_id,
                "scoring profile",
            )
            if character is not None and character.data_set_id != selected_data_set_id:
                raise ValueError("assigned character must belong to the capture data set")
            if profile is not None and profile.data_set_id not in {
                None,
                selected_data_set_id,
            }:
                raise ValueError("scoring profile must belong to the capture data set")

            stale_records = list(
                session.scalars(
                    select(CaptureSessionRecord).where(
                        CaptureSessionRecord.data_set_id == selected_data_set_id,
                        CaptureSessionRecord.status == "active",
                    )
                ).all()
            )
            for stale in stale_records:
                stale.status = "interrupted"
                stale.stopped_at = when
                if not stale.error_summary:
                    stale.error_summary = "superseded by a new capture session"

            record = CaptureSessionRecord(
                data_set_id=selected_data_set_id,
                assigned_character_id=assigned_character_id,
                scoring_profile_id=scoring_profile_id,
                backend_kind=normalized_backend,
                monitor_index=monitor_index,
                sampling_hz=float(sampling_hz),
                stable_wait_ms=stable_wait_ms,
                status="active",
                started_at=when,
                app_version=normalized_version,
                roi_config_version=_optional_text(
                    roi_config_version,
                    "roi_config_version",
                    maximum=80,
                ),
                metadata_json=safe_metadata,
            )
            session.add(record)
            session.flush()
            return _capture_snapshot(record)

    def stop_capture(
        self,
        capture_session_id: str,
        *,
        status: str = "stopped",
        error_summary: str | None = None,
        stopped_at: datetime | None = None,
    ) -> CaptureSessionSnapshot:
        """Stop a capture idempotently; confirmed history remains durable."""

        normalized_status = status.strip().lower()
        if normalized_status not in _CAPTURE_STOP_STATUSES:
            raise ValueError(f"unsupported capture stop status: {status}")
        normalized_error = _optional_text(error_summary, "error_summary", maximum=4000)
        when = _as_utc(stopped_at or utc_now())
        with self._session_factory.begin() as session:
            record = _require_record(session, CaptureSessionRecord, capture_session_id, "capture")
            if record.status == "active":
                record.status = normalized_status
                record.stopped_at = when
                record.error_summary = normalized_error
            elif normalized_error and not record.error_summary:
                record.error_summary = normalized_error
            session.flush()
            return _capture_snapshot(record)

    def ingest_live_result(
        self,
        capture_session_id: str,
        result: Mapping[str, object],
        *,
        source_sequence: int | None = None,
        occurred_at: datetime | None = None,
        confidence_threshold: float = 0.75,
    ) -> IngestReceipt:
        """Atomically persist one array-free ``AnalysisResult.observation`` mapping.

        ``result`` may be ``analysis.observation`` directly, a serialized
        ``AnalysisResult``, or a serialized ``LiveRecognitionResult``.  No live DTO
        import is required. It may contain ``events`` and ``current_session``
        alongside observation fields. Repeating the same nonnegative
        ``source_sequence`` is idempotent.
        """

        if not isfinite(confidence_threshold) or not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be within [0, 1]")
        safe_result = _json_object(result, label="live result", reject_images=True)
        safe_result, source_sequence, occurred_at = _unwrap_live_result(
            safe_result,
            source_sequence=source_sequence,
            occurred_at=occurred_at,
        )
        if source_sequence is not None and source_sequence < 0:
            raise ValueError("source_sequence must be nonnegative")
        if "_ingest" in safe_result:
            raise ValueError("live result uses reserved key _ingest")
        events_payload = safe_result.pop("events", [])
        current_session_payload = safe_result.pop("current_session", None)
        events = _json_list_of_objects(events_payload, label="events")
        current_summary = _optional_json_object(current_session_payload, label="current_session")
        observation_payload = {
            key: value for key, value in safe_result.items() if key in _OBSERVATION_FIELDS
        }
        unknown = set(safe_result).difference(_OBSERVATION_FIELDS)
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise ValueError(f"live result contains unsupported fields: {rendered}")
        page_type = _page_type(observation_payload.get("page_type"))
        owner_name = _optional_text(
            observation_payload.get("equipped_character_name"),
            "equipped_character_name",
            maximum=120,
        )
        _optional_text(observation_payload.get("set_name"), "set_name", maximum=160)
        slot_index = _optional_positive_int(
            observation_payload.get("equipped_slot_index"), "equipped_slot_index"
        )
        if slot_index is not None and slot_index > 5:
            raise ValueError("equipped_slot_index must be within [1, 5]")
        current_local_session_id = (
            _required_text(
                current_summary.get("local_session_id"),
                "current_session.local_session_id",
                maximum=80,
            )
            if current_summary is not None
            else None
        )
        selection_identity = _validate_revisit_metadata(
            observation_payload,
            page_type=page_type,
            current_local_session_id=current_local_session_id,
        )
        when = _as_utc(occurred_at or utc_now())

        with self._session_factory.begin() as session:
            capture = _require_record(
                session,
                CaptureSessionRecord,
                capture_session_id,
                "capture",
            )
            if capture.status != "active":
                raise RuntimeError(f"capture session is not active: {capture.status}")

            duplicate = _find_duplicate_ingest(session, capture_session_id, source_sequence)
            if duplicate is not None:
                return duplicate

            current_session: EchoSessionRecord | None = None
            observation: EchoObservationRecord | None = None
            created_character_id: str | None = None
            equipment_changed = False
            if current_summary is not None:
                current_session = _upsert_echo_session(
                    session,
                    capture,
                    current_summary,
                    observation_payload,
                    when,
                )
                reused_revisit = (
                    page_type == PageType.ECHO_SELECTION.value
                    and observation_payload.get("revisit_status") == "reused"
                    and observation_payload.get("revisit_session_id")
                    == current_session.local_session_id
                    and selection_identity is not None
                )
                if reused_revisit:
                    if selection_identity is None:  # pragma: no cover - guarded above
                        raise ValueError("reused revisit requires selection identity")
                    if not _matches_latest_persisted_selection_identity(
                        session,
                        current_session,
                        selection_identity,
                    ):
                        raise ValueError(
                            "reused revisit evidence does not match persisted selection identity"
                        )
                duplicate_revisit = reused_revisit and _matches_latest_structured_state(
                    session,
                    current_session,
                    observation_payload,
                )
                if not duplicate_revisit:
                    observation = _add_observation(
                        session,
                        current_session,
                        page_type,
                        observation_payload,
                        source_sequence=source_sequence,
                        observed_at=when,
                        confidence_threshold=confidence_threshold,
                    )
                _update_echo_from_payload(
                    session,
                    current_session,
                    observation_payload,
                    preserve_confirmed=True,
                )
                owner_confidence = (
                    _optional_json_object(observation_payload.get("confidence"), label="confidence")
                    or {}
                )
                name_confidence = _optional_confidence(
                    owner_confidence.get("equipped_character_name"), "owner confidence"
                )
                if (
                    page_type == PageType.ECHO_SELECTION.value
                    and observation_payload.get("is_equipped") is True
                    and owner_name is not None
                    and name_confidence is not None
                    and name_confidence >= max(0.90, confidence_threshold)
                ):
                    slot_confidence = _optional_confidence(
                        owner_confidence.get("equipped_slot_index"), "slot confidence"
                    )
                    registration = register_recognized_equipment(
                        session,
                        current_session,
                        data_set_id=capture.data_set_id,
                        name=owner_name,
                        slot_index=(
                            slot_index
                            if slot_confidence is not None
                            and slot_confidence >= max(0.90, confidence_threshold)
                            else None
                        ),
                    )
                    equipment_changed = registration.changed
                    if registration.created:
                        created_character_id = registration.character_id
                elif (
                    page_type == PageType.ECHO_SELECTION.value
                    and observation_payload.get("is_equipped") is False
                    and _explicit_unequip_action(observation_payload, confidence_threshold)
                ):
                    equipment_changed = clear_recognized_equipment(session, current_session)

            event_records = _add_events(
                session,
                capture,
                events,
                current_session=current_session,
                current_observation=observation,
                source_sequence=source_sequence,
                occurred_at=when,
            )
            session.flush()
            return IngestReceipt(
                capture_session_id=capture.id,
                echo_session_id=current_session.id if current_session is not None else None,
                observation_id=observation.id if observation is not None else None,
                event_ids=tuple(record.id for record in event_records),
                duplicate=False,
                created_character_id=created_character_id,
                equipment_changed=equipment_changed,
            )

    def correct_observation(
        self,
        observation_id: str,
        field_path: str,
        new_value: object,
        *,
        reason: str | None = None,
        actor: str = "user",
        mark_reviewed: bool = False,
    ) -> CorrectionSnapshot:
        """Apply a JSON-pointer correction while preserving raw OCR and an audit row."""

        pointer = _parse_correction_pointer(field_path)
        safe_value = _json_value(new_value, label="correction value", reject_images=True)
        normalized_reason = _optional_text(reason, "reason", maximum=4000)
        normalized_actor = _required_text(actor, "actor", maximum=40)

        with self._session_factory.begin() as session:
            observation = _require_record(
                session,
                EchoObservationRecord,
                observation_id,
                "observation",
            )
            effective = deepcopy(observation.effective_payload_json)
            old_value = _apply_correction(effective, pointer, safe_value)
            _validate_effective_observation(effective)
            if mark_reviewed:
                confirmed = set(confirmed_fields(effective))
                confirmed.add(field_path)
                effective["manual_confirmed_fields"] = sorted(confirmed)

            audit = ObservationCorrectionRecord(
                observation_id=observation.id,
                field_path=field_path,
                old_value_json=old_value,
                new_value_json=safe_value,
                reason=normalized_reason,
                actor=normalized_actor,
            )
            session.add(audit)
            observation.effective_payload_json = effective
            observation.payload_version += 1
            if mark_reviewed:
                observation.requires_review = bool(observation_review_issues(effective))
            _sync_observation_columns(observation, effective)

            echo_session = _require_record(
                session,
                EchoSessionRecord,
                observation.echo_session_id,
                "echo session",
            )
            latest_id = session.scalar(
                select(EchoObservationRecord.id)
                .where(EchoObservationRecord.echo_session_id == echo_session.id)
                .order_by(EchoObservationRecord.sequence_no.desc())
                .limit(1)
            )
            if latest_id == observation.id:
                _update_echo_from_payload(
                    session,
                    echo_session,
                    effective,
                    preserve_confirmed=False,
                )
            session.flush()
            return _correction_snapshot(audit)

    def set_disposition(
        self,
        echo_session_id: str,
        disposition: EchoDisposition | str,
        *,
        character_id: str | None = None,
        slot_index: int | None = None,
        replace: bool = False,
    ) -> EchoSessionSummary:
        """Store a user-confirmed outcome and optional permanent character slot link."""

        try:
            normalized = EchoDisposition(disposition)
        except ValueError as exc:
            raise ValueError(f"unsupported echo disposition: {disposition}") from exc
        if slot_index is not None and (isinstance(slot_index, bool) or not 1 <= slot_index <= 5):
            raise ValueError("slot_index must be within [1, 5]")
        if not isinstance(replace, bool):
            raise ValueError("replace must be a boolean")
        if normalized is EchoDisposition.EQUIPPED and character_id is None:
            raise ValueError("equipped disposition requires character_id")
        if character_id is None and slot_index is not None:
            raise ValueError("slot_index requires character_id")

        with self._session_factory.begin() as session:
            echo_session = _require_record(
                session,
                EchoSessionRecord,
                echo_session_id,
                "echo session",
            )
            echo = _require_record(session, EchoRecord, echo_session.echo_id, "echo")
            if echo_session.was_consumed:
                raise ValueError("cannot change the disposition of a consumed Echo")
            if character_id is not None:
                character = _require_record(session, CharacterRecord, character_id, "character")
                capture = _require_record(
                    session,
                    CaptureSessionRecord,
                    echo_session.capture_session_id,
                    "capture",
                )
                if character.data_set_id != capture.data_set_id:
                    raise ValueError("character and echo must belong to the same data set")
                if character.archived_at is not None:
                    raise ValueError("cannot equip an Echo to an archived character")
                if normalized is EchoDisposition.EQUIPPED and slot_index is not None:
                    assign_echo_record(
                        session,
                        character_id,
                        slot_index,
                        echo.id,
                        data_set_id=capture.data_set_id,
                        replace=replace,
                    )
                    return _session_summary(session, echo_session)

            if normalized is EchoDisposition.KEPT and echo.character_id is not None:
                # Keeping a previously linked echo must not silently remove its
                # character slot. Explicit discard/unassignment handles removal.
                echo.final_status = "equipped" if echo.slot_index is not None else "kept"
                echo.archived_at = None
                echo.updated_at = utc_now()
                session.flush()
                return _session_summary(session, echo_session)

            echo.final_status = normalized.value
            if normalized is EchoDisposition.DISCARDED:
                echo.character_id = None
                echo.slot_index = None
                echo.equipped_at = None
                echo.archived_at = utc_now()
            else:
                echo.character_id = character_id
                echo.slot_index = slot_index
                echo.archived_at = None
                echo.equipped_at = utc_now() if normalized is EchoDisposition.EQUIPPED else None
            if character_id is not None:
                for related in session.scalars(
                    select(EchoSessionRecord).where(EchoSessionRecord.echo_id == echo.id)
                ):
                    related.metadata_json = {
                        **related.metadata_json,
                        "equipment_assignment_source": "manual",
                    }
            session.flush()
            return _session_summary(session, echo_session)

    def list_history(
        self,
        *,
        data_set_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        group_duplicates: bool = False,
    ) -> tuple[EchoSessionSummary, ...]:
        """List newest sessions, optionally folding exact rescans before pagination.

        Folded rows retain a real representative session for history and editing.
        All original session identifiers remain available for navigation; neither
        this read nor grouping changes stored observations or temporal lineage.
        """

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be within [1, 1000]")
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        return self._read_history(
            data_set_id=data_set_id, limit=limit, offset=offset, group_duplicates=group_duplicates
        )

    def list_history_snapshot(
        self, *, data_set_id: str | None = None, group_duplicates: bool = False
    ) -> tuple[EchoSessionSummary, ...]:
        """Read the complete table once, folding rescans once per refresh.

        The Qt table already uses a lazy item model. Return immutable summaries
        in one read transaction so large histories need no repeated full scans
        and inserts between batches cannot shift or duplicate a page boundary.
        """
        return self._read_history(
            data_set_id=data_set_id, limit=None, offset=0, group_duplicates=group_duplicates
        )

    def _read_history(
        self, *, data_set_id: str | None, limit: int | None, offset: int, group_duplicates: bool
    ) -> tuple[EchoSessionSummary, ...]:
        with self._session_factory() as session:
            # sqlite3's legacy transaction mode does not BEGIN for SELECT. Keep
            # the session list, grouping and batched summaries on one snapshot.
            session.connection().exec_driver_sql("BEGIN")
            statement = (
                select(EchoSessionRecord, CaptureSessionRecord.data_set_id)
                .join(CaptureSessionRecord)
                .order_by(EchoSessionRecord.started_at.desc(), EchoSessionRecord.id.desc())
            )
            if data_set_id is not None:
                statement = statement.where(CaptureSessionRecord.data_set_id == data_set_id)
            if not group_duplicates:
                records = session.scalars(statement.limit(limit).offset(offset)).all()
                return _session_summaries(session, records)

            rows = list(session.execute(statement).all())
            records_by_echo: dict[str, list[EchoSessionRecord]] = {}
            session_order = {record.id: index for index, (record, _) in enumerate(rows)}
            for record, _dataset in rows:
                if record.echo_id is not None:
                    records_by_echo.setdefault(record.echo_id, []).append(record)
            folded_by_echo: dict[str, tuple[EchoSessionRecord, int, tuple[str, ...]]] = {}
            for dataset in {dataset for _record, dataset in rows}:
                for group in group_echo_records(session, data_set_id=dataset):
                    representatives = records_by_echo.get(group.representative_echo_id)
                    if not group.rescan_count or not representatives:
                        # A manually created representative may have no capture
                        # history. Keep its scanned members visible and editable.
                        continue
                    member_ids = group.member_echo_ids
                    session_ids = tuple(
                        sorted(
                            (
                                record.id
                                for echo_id in member_ids
                                for record in records_by_echo.get(echo_id, ())
                            ),
                            key=session_order.__getitem__,
                        )
                    )
                    folded = (representatives[0], group.rescan_count, session_ids)
                    folded_by_echo.update({echo_id: folded for echo_id in member_ids})

            selected: list[tuple[EchoSessionRecord, int, tuple[str, ...]]] = []
            emitted: set[str] = set()
            for record, _dataset in rows:
                representative, rescans, members = folded_by_echo.get(
                    record.echo_id or "", (record, 0, ())
                )
                if representative.id in emitted:
                    continue
                emitted.add(representative.id)
                selected.append((representative, rescans, members))
            selected = selected[offset : None if limit is None else offset + limit]
            summaries = _session_summaries(session, [record for record, _, _ in selected])
            return tuple(
                replace(
                    summary,
                    rescan_count=rescans,
                    grouped_session_ids=members,
                )
                for summary, (_, rescans, members) in zip(summaries, selected, strict=True)
            )

    def get_history(self, echo_session_id: str) -> EchoHistory:
        """Load one complete echo lineage including immutable correction audit."""

        with self._session_factory() as session:
            echo_session = _require_record(
                session,
                EchoSessionRecord,
                echo_session_id,
                "echo session",
            )
            capture = _require_record(
                session,
                CaptureSessionRecord,
                echo_session.capture_session_id,
                "capture",
            )
            observations = list(
                session.scalars(
                    select(EchoObservationRecord)
                    .where(EchoObservationRecord.echo_session_id == echo_session.id)
                    .order_by(EchoObservationRecord.sequence_no)
                ).all()
            )
            observation_ids = [item.id for item in observations]
            event_scope = or_(
                EchoEventRecord.echo_session_id == echo_session.id,
                EchoEventRecord.related_session_id == echo_session.id,
            )
            if observation_ids:
                # Page-change events deliberately have no logical session id,
                # but their current-observation link still makes them
                # attributable to one echo. Keep those while excluding
                # unrelated echoes from the same capture run.
                event_scope = or_(
                    event_scope,
                    EchoEventRecord.current_observation_id.in_(observation_ids),
                )
            events = list(
                session.scalars(
                    select(EchoEventRecord)
                    .where(
                        EchoEventRecord.capture_session_id == capture.id,
                        event_scope,
                    )
                    .order_by(EchoEventRecord.sequence_no)
                ).all()
            )
            corrections: list[ObservationCorrectionRecord] = []
            if observation_ids:
                corrections = list(
                    session.scalars(
                        select(ObservationCorrectionRecord)
                        .where(ObservationCorrectionRecord.observation_id.in_(observation_ids))
                        .order_by(ObservationCorrectionRecord.corrected_at)
                    ).all()
                )
            return EchoHistory(
                session=_session_summary(session, echo_session),
                capture=_capture_snapshot(capture),
                observations=tuple(_observation_snapshot(item) for item in observations),
                events=tuple(_event_snapshot(item) for item in events),
                corrections=tuple(_correction_snapshot(item) for item in corrections),
            )


def _matches_latest_structured_state(
    session: Session,
    echo_session: EchoSessionRecord,
    payload: Mapping[str, Any],
) -> bool:
    latest = session.scalar(
        select(EchoObservationRecord)
        .where(EchoObservationRecord.echo_session_id == echo_session.id)
        .order_by(EchoObservationRecord.sequence_no.desc())
        .limit(1)
    )
    if latest is None:
        return False
    return _structured_state_key(latest.effective_payload_json) == _structured_state_key(payload)


def _matches_latest_persisted_selection_identity(
    session: Session,
    echo_session: EchoSessionRecord,
    incoming: SelectionIdentityEvidence,
) -> bool:
    """Match against the latest durable visual evidence, never caller claims alone."""

    observations = session.scalars(
        select(EchoObservationRecord)
        .where(
            EchoObservationRecord.echo_session_id == echo_session.id,
            EchoObservationRecord.page_type == PageType.ECHO_SELECTION.value,
        )
        .order_by(EchoObservationRecord.sequence_no.desc())
    )
    for observation in observations:
        persisted_value = observation.effective_payload_json.get("selection_identity")
        if persisted_value is None:
            continue
        try:
            persisted = _validate_selection_identity(persisted_value)
        except ValueError:
            return False
        return persisted is not None and visual_identity_matches(persisted, incoming)
    return False


def _explicit_unequip_action(payload: Mapping[str, Any], threshold: float) -> bool:
    raw = payload.get("raw_ocr")
    tokens = raw.get("selection_primary_action") if isinstance(raw, dict) else None
    if not isinstance(tokens, list):
        return False
    actions = {
        token.get("text", "").strip()
        for token in tokens
        if isinstance(token, dict)
        and isinstance(token.get("text"), str)
        and isinstance(token.get("confidence"), (int, float))
        and not isinstance(token.get("confidence"), bool)
        and max(0.90, threshold) <= token["confidence"] <= 1
    }
    return actions == {"裝配"}


def _structured_state_key(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    def stat_key(field: str) -> tuple[tuple[Any, Any, Any], ...]:
        value = payload.get(field)
        if not isinstance(value, list):
            return ()
        return tuple(
            (item.get("name"), item.get("value"), item.get("unit"))
            for item in value
            if isinstance(item, dict)
        )

    return (
        payload.get("page_type"),
        payload.get("name"),
        payload.get("set_name"),
        payload.get("level"),
        payload.get("projected_level"),
        payload.get("cost"),
        stat_key("main_stats"),
        stat_key("projected_main_stats"),
        stat_key("sub_stats"),
        payload.get("locked_substat_slots"),
        payload.get("is_discarded"),
        payload.get("is_equipped"),
        payload.get("equipped_character_name"),
        payload.get("equipped_slot_index"),
        payload.get("material_source_selected"),
        payload.get("material_source_level"),
    )


def _upsert_echo_session(
    session: Session,
    capture: CaptureSessionRecord,
    summary: dict[str, Any],
    observation_payload: dict[str, Any],
    occurred_at: datetime,
) -> EchoSessionRecord:
    local_session_id = _required_text(
        summary.get("local_session_id"),
        "current_session.local_session_id",
        maximum=80,
    )
    state = _required_text(
        summary.get("current_state", SessionState.ACTIVE.value),
        "current_session.current_state",
        maximum=40,
    )
    if state not in {item.value for item in SessionState}:
        raise ValueError(f"unsupported echo session state: {state}")
    record = session.scalar(
        select(EchoSessionRecord).where(
            EchoSessionRecord.capture_session_id == capture.id,
            EchoSessionRecord.local_session_id == local_session_id,
        )
    )
    if record is None:
        echo = EchoRecord(data_set_id=capture.data_set_id)
        session.add(echo)
        session.flush()
        record = EchoSessionRecord(
            capture_session_id=capture.id,
            echo_id=echo.id,
            local_session_id=local_session_id,
            started_at=occurred_at,
        )
        session.add(record)
        session.flush()

    record.current_state = state
    if "is_discarded" in summary:
        record.is_discarded = _optional_bool(summary.get("is_discarded"), default=False)
    if "was_consumed" in summary:
        record.was_consumed = _optional_bool(summary.get("was_consumed"), default=False)
    if state == SessionState.CONSUMED_ENDED.value and record.ended_at is None:
        record.ended_at = occurred_at
    del observation_payload
    return record


def _add_observation(
    session: Session,
    echo_session: EchoSessionRecord,
    page_type: str,
    payload: dict[str, Any],
    *,
    source_sequence: int | None,
    observed_at: datetime,
    confidence_threshold: float,
) -> EchoObservationRecord:
    safe_payload = deepcopy(payload)
    safe_payload["_ingest"] = {"source_sequence": source_sequence}
    confidence = _confidence_map(payload.get("confidence", {}))
    overall_confidence = min(confidence.values()) if confidence else None
    source_file = payload.get("source_file")
    if source_file is not None:
        source_file = Path(_required_text(source_file, "source_file")).name
        safe_payload["source_file"] = source_file
    sequence_no = (
        int(
            session.scalar(
                select(func.coalesce(func.max(EchoObservationRecord.sequence_no), 0)).where(
                    EchoObservationRecord.echo_session_id == echo_session.id
                )
            )
            or 0
        )
        + 1
    )
    record = EchoObservationRecord(
        echo_session_id=echo_session.id,
        sequence_no=sequence_no,
        page_type=page_type,
        raw_payload_json=deepcopy(safe_payload),
        effective_payload_json=deepcopy(safe_payload),
        overall_confidence=overall_confidence,
        confidence_json=confidence,
        source_timestamp=_optional_text(
            payload.get("source_timestamp"),
            "source_timestamp",
            maximum=80,
        ),
        source_file=source_file,
        semantic_fingerprint=_optional_text(
            payload.get("semantic_fingerprint"),
            "semantic_fingerprint",
            maximum=500,
        ),
        requires_review=_observation_requires_review(
            page_type,
            payload,
            overall_confidence=overall_confidence,
            confidence_threshold=confidence_threshold,
        ),
        observed_at=observed_at,
    )
    _sync_observation_columns(record, safe_payload)
    session.add(record)
    session.flush()
    return record


def _validate_selection_identity(value: object) -> SelectionIdentityEvidence | None:
    if value is None:
        return None
    identity = _json_object(value, label="selection_identity")
    expected = {
        "version",
        "selected_cell",
        "selected_card_bbox",
        "card_art_phash",
        "context_phashes",
    }
    if set(identity) != expected:
        raise ValueError("selection_identity has unsupported or missing fields")
    version = _required_text(identity.get("version"), "selection_identity.version", maximum=40)
    if version != "selection-grid-v1":
        raise ValueError("unsupported selection_identity.version")
    selected_cell = _optional_nonnegative_int(
        identity.get("selected_cell"),
        "selection_identity.selected_cell",
    )
    if selected_cell is None or not 1 <= selected_cell <= 15:
        raise ValueError("selection_identity.selected_cell must be within [1, 15]")
    bbox = identity.get("selected_card_bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in bbox)
        or any(item < 0 for item in bbox)
        or not 130 <= bbox[2] <= 175
        or not 170 <= bbox[3] <= 220
    ):
        raise ValueError("selection_identity.selected_card_bbox must be a non-empty integer bbox")
    cell_index = selected_cell - 1
    expected_x = 5 + 179 * (cell_index % 3)
    expected_y = 8 + 220 * (cell_index // 3)
    if abs(bbox[0] - expected_x) > 14 or abs(bbox[1] - expected_y) > 14:
        raise ValueError("selection_identity bbox does not match selected_cell")
    card_hash = _required_text(
        identity.get("card_art_phash"),
        "selection_identity.card_art_phash",
        maximum=16,
    )
    if len(card_hash) != 16:
        raise ValueError("selection_identity.card_art_phash must contain 16 hex characters")
    hashes = identity.get("context_phashes")
    if (
        not isinstance(hashes, list)
        or len(hashes) != 15
        or not all(isinstance(item, str) and len(item) == 16 for item in hashes)
    ):
        raise ValueError("selection_identity.context_phashes must contain 15 hashes")
    try:
        int(card_hash, 16)
        for item in hashes:
            int(item, 16)
    except ValueError as exc:
        raise ValueError("selection_identity hashes must be hexadecimal") from exc
    return SelectionIdentityEvidence(
        version=version,
        selected_cell=selected_cell,
        selected_card_bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
        card_art_phash=card_hash,
        context_phashes=tuple(hashes),
    )


def _validate_revisit_metadata(
    payload: Mapping[str, Any],
    *,
    page_type: str,
    current_local_session_id: str | None,
) -> SelectionIdentityEvidence | None:
    """Fail closed unless revisit metadata came with selection-page visual evidence."""

    identity = payload.get("selection_identity")
    validated_identity = _validate_selection_identity(identity)
    revisit_status = _optional_text(payload.get("revisit_status"), "revisit_status", maximum=20)
    if revisit_status not in {None, "reused", "possible"}:
        raise ValueError("revisit_status must be reused, possible, or null")
    revisit_session_id = _optional_text(
        payload.get("revisit_session_id"),
        "revisit_session_id",
        maximum=80,
    )

    if revisit_status is None:
        if revisit_session_id is not None:
            raise ValueError("revisit_session_id requires revisit_status")
        return validated_identity
    if page_type != PageType.ECHO_SELECTION.value:
        raise ValueError("revisit metadata is only valid for echo_selection")
    if identity is None:
        raise ValueError("revisit metadata requires selection_identity")
    if current_local_session_id is None:
        raise ValueError("revisit metadata requires current_session")
    if revisit_status == "reused":
        if revisit_session_id is None:
            raise ValueError("reused revisit requires revisit_session_id")
        if revisit_session_id != current_local_session_id:
            raise ValueError("reused revisit must target current_session")
    return validated_identity


def _observation_requires_review(
    page_type: str,
    payload: Mapping[str, Any],
    *,
    overall_confidence: float | None,
    confidence_threshold: float,
) -> bool:
    """Reject confident-looking but structurally incomplete Echo observations."""

    if overall_confidence is None or overall_confidence < confidence_threshold:
        return True
    if page_type not in {
        PageType.ECHO_SELECTION.value,
        PageType.ECHO_ENHANCEMENT.value,
    }:
        # Material-selection has transition-specific semantics (current and
        # projected values plus an optional source) and intentionally keeps its
        # existing confidence gate.  In particular, do not apply the target's
        # current level to the projected unlocked-substat count.
        return False

    name = payload.get("name")
    level = payload.get("level")
    if (
        not isinstance(name, str)
        or not name.strip()
        or isinstance(level, bool)
        or not isinstance(level, int)
        or level not in _ECHO_LEVEL_STAGES
    ):
        return True
    if not _has_resolved_stats(payload.get("main_stats"), expected_count=2):
        return True

    expected_substats = level // 5
    if not _has_resolved_stats(
        payload.get("sub_stats"),
        expected_count=expected_substats,
    ):
        return True

    if page_type == PageType.ECHO_SELECTION.value:
        cost = payload.get("cost")
        return isinstance(cost, bool) or cost not in {1, 3, 4}

    locked = payload.get("locked_substat_slots")
    return (
        isinstance(locked, bool) or not isinstance(locked, int) or locked != 5 - expected_substats
    )


def _has_resolved_stats(value: object, *, expected_count: int) -> bool:
    if not isinstance(value, list) or len(value) != expected_count:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        number = item.get("value")
        if not isinstance(name, str) or not name.strip():
            return False
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not isfinite(float(number))
        ):
            return False
    return True


def _add_events(
    session: Session,
    capture: CaptureSessionRecord,
    events: list[dict[str, Any]],
    *,
    current_session: EchoSessionRecord | None,
    current_observation: EchoObservationRecord | None,
    source_sequence: int | None,
    occurred_at: datetime,
) -> list[EchoEventRecord]:
    next_sequence = (
        int(
            session.scalar(
                select(func.coalesce(func.max(EchoEventRecord.sequence_no), 0)).where(
                    EchoEventRecord.capture_session_id == capture.id
                )
            )
            or 0
        )
        + 1
    )
    records: list[EchoEventRecord] = []
    for payload in events:
        event_type = _required_text(payload.get("event_type"), "event_type", maximum=80)
        if event_type not in {item.value for item in EventType}:
            raise ValueError(f"unsupported echo event type: {event_type}")
        local_session_id = _optional_text(payload.get("session_id"), "session_id", maximum=80)
        related_local_id = _optional_text(
            payload.get("related_session_id"),
            "related_session_id",
            maximum=80,
        )
        event_session = _resolve_local_session(
            session,
            capture,
            local_session_id,
            current_session=current_session,
            fallback_payload=(
                payload.get("previous_observation") or payload.get("current_observation")
            ),
            occurred_at=occurred_at,
        )
        related_session = _resolve_local_session(
            session,
            capture,
            related_local_id,
            current_session=current_session,
            fallback_payload=payload.get("current_observation"),
            occurred_at=occurred_at,
        )
        confidence = _optional_confidence(payload.get("confidence"), "event confidence")
        notes_value = payload.get("notes", [])
        if not isinstance(notes_value, list) or not all(
            isinstance(item, str) for item in notes_value
        ):
            raise ValueError("event notes must be a list of strings")
        safe_event = deepcopy(payload)
        safe_event["_ingest"] = {"source_sequence": source_sequence}
        previous_observation = _previous_observation_for_event(
            session,
            capture,
            event_session,
            current_observation,
        )
        record = EchoEventRecord(
            capture_session_id=capture.id,
            echo_session_id=event_session.id if event_session is not None else None,
            related_session_id=related_session.id if related_session is not None else None,
            previous_observation_id=(
                previous_observation.id if previous_observation is not None else None
            ),
            current_observation_id=(
                current_observation.id if current_observation is not None else None
            ),
            sequence_no=next_sequence,
            event_type=event_type,
            confidence=confidence,
            notes_json=list(notes_value),
            payload_json=safe_event,
            occurred_at=occurred_at,
        )
        session.add(record)
        records.append(record)
        next_sequence += 1

        if event_session is not None:
            if event_type == EventType.ECHO_MARKED_DISCARDED.value:
                event_session.is_discarded = True
                event_session.current_state = SessionState.DISCARDED_ACTIVE.value
            elif event_type == EventType.ECHO_USED_AS_MATERIAL.value:
                event_session.was_consumed = True
                event_session.current_state = SessionState.CONSUMED_ENDED.value
                event_session.ended_at = occurred_at
                event_session.consumed_into_session_id = (
                    related_session.id if related_session is not None else None
                )
                consumed_echo = session.get(EchoRecord, event_session.echo_id)
                if consumed_echo is not None:
                    consumed_echo.character_id = None
                    consumed_echo.slot_index = None
                    consumed_echo.final_status = "consumed"
                    consumed_echo.equipped_at = None
                    consumed_echo.archived_at = occurred_at
                    consumed_echo.updated_at = utc_now()
            elif event_type == EventType.ECHO_SESSION_ENDED.value:
                event_session.ended_at = occurred_at
    return records


def _resolve_local_session(
    session: Session,
    capture: CaptureSessionRecord,
    local_session_id: str | None,
    *,
    current_session: EchoSessionRecord | None,
    fallback_payload: object,
    occurred_at: datetime,
) -> EchoSessionRecord | None:
    if local_session_id is None:
        return None
    if current_session is not None and current_session.local_session_id == local_session_id:
        return current_session
    record = session.scalar(
        select(EchoSessionRecord).where(
            EchoSessionRecord.capture_session_id == capture.id,
            EchoSessionRecord.local_session_id == local_session_id,
        )
    )
    if record is not None:
        return record
    payload = _optional_json_object(fallback_payload, label="event observation") or {}
    summary = {
        "local_session_id": local_session_id,
        "current_state": SessionState.ACTIVE.value,
    }
    return _upsert_echo_session(session, capture, summary, payload, occurred_at)


def _previous_observation_for_event(
    session: Session,
    capture: CaptureSessionRecord,
    event_session: EchoSessionRecord | None,
    current: EchoObservationRecord | None,
) -> EchoObservationRecord | None:
    if event_session is not None:
        statement = (
            select(EchoObservationRecord)
            .where(EchoObservationRecord.echo_session_id == event_session.id)
            .order_by(EchoObservationRecord.sequence_no.desc())
        )
        if current is not None and current.echo_session_id == event_session.id:
            statement = statement.where(EchoObservationRecord.id != current.id)
        return session.scalar(statement.limit(1))
    statement = (
        select(EchoObservationRecord)
        .join(EchoSessionRecord)
        .where(EchoSessionRecord.capture_session_id == capture.id)
        .order_by(
            EchoObservationRecord.observed_at.desc(),
            EchoObservationRecord.sequence_no.desc(),
        )
    )
    if current is not None:
        statement = statement.where(EchoObservationRecord.id != current.id)
    return session.scalar(statement.limit(1))


def _find_duplicate_ingest(
    session: Session,
    capture_session_id: str,
    source_sequence: int | None,
) -> IngestReceipt | None:
    if source_sequence is None:
        return None
    observations = list(
        session.scalars(
            select(EchoObservationRecord)
            .join(EchoSessionRecord)
            .where(EchoSessionRecord.capture_session_id == capture_session_id)
        ).all()
    )
    matched_observation = next(
        (
            item
            for item in observations
            if _ingest_sequence(item.raw_payload_json) == source_sequence
        ),
        None,
    )
    events = list(
        session.scalars(
            select(EchoEventRecord).where(EchoEventRecord.capture_session_id == capture_session_id)
        ).all()
    )
    matched_events = [
        item for item in events if _ingest_sequence(item.payload_json) == source_sequence
    ]
    if matched_observation is None and not matched_events:
        return None
    echo_session_id = (
        matched_observation.echo_session_id
        if matched_observation is not None
        else matched_events[0].echo_session_id
    )
    return IngestReceipt(
        capture_session_id=capture_session_id,
        echo_session_id=echo_session_id,
        observation_id=matched_observation.id if matched_observation is not None else None,
        event_ids=tuple(item.id for item in matched_events),
        duplicate=True,
    )


def _ingest_sequence(payload: Mapping[str, Any]) -> int | None:
    metadata = payload.get("_ingest")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("source_sequence")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _update_echo_from_payload(
    session: Session,
    echo_session: EchoSessionRecord,
    payload: Mapping[str, Any],
    *,
    preserve_confirmed: bool,
) -> None:
    echo = _require_record(session, EchoRecord, echo_session.echo_id, "echo")
    name = _optional_text(payload.get("name"), "name", maximum=160)
    if name is not None or not preserve_confirmed:
        echo.name = name
    set_name = payload.get("set_name")
    if set_name is not None:
        echo.set_name = _optional_text(set_name, "set_name", maximum=160)
    level = _optional_nonnegative_int(payload.get("level"), "level")
    cost = _optional_positive_int(payload.get("cost"), "cost")
    if level is not None or not preserve_confirmed:
        echo.level = level
    if cost is not None or not preserve_confirmed:
        echo.cost = cost
    main_stats = _stat_list(payload.get("main_stats", []), "main_stats")
    sub_stats = _stat_list(payload.get("sub_stats", []), "sub_stats")
    if main_stats or not preserve_confirmed:
        echo.main_stats_json = main_stats
    if sub_stats or level == 0 or not preserve_confirmed:
        echo.sub_stats_json = sub_stats


def _sync_observation_columns(
    record: EchoObservationRecord,
    payload: Mapping[str, Any],
) -> None:
    record.name = _optional_text(payload.get("name"), "name", maximum=160)
    record.level = _optional_nonnegative_int(payload.get("level"), "level")
    record.projected_level = _optional_nonnegative_int(
        payload.get("projected_level"),
        "projected_level",
    )
    record.cost = _optional_positive_int(payload.get("cost"), "cost")
    record.main_stats_json = _stat_list(payload.get("main_stats", []), "main_stats")
    record.projected_main_stats_json = _stat_list(
        payload.get("projected_main_stats", []),
        "projected_main_stats",
    )
    record.sub_stats_json = _stat_list(payload.get("sub_stats", []), "sub_stats")
    record.locked_substat_slots = _optional_nonnegative_int(
        payload.get("locked_substat_slots"),
        "locked_substat_slots",
    )


def _validate_effective_observation(payload: Mapping[str, Any]) -> None:
    _page_type(payload.get("page_type"))
    temporary = EchoObservationRecord(
        echo_session_id="validation-only",
        sequence_no=1,
        page_type=str(payload.get("page_type")),
        confidence_json={},
        raw_payload_json={},
        effective_payload_json={},
    )
    _sync_observation_columns(temporary, payload)
    _optional_text(payload.get("equipped_character_name"), "equipped_character_name", maximum=120)
    _optional_text(payload.get("set_name"), "set_name", maximum=160)
    slot = _optional_positive_int(payload.get("equipped_slot_index"), "equipped_slot_index")
    if slot is not None and slot > 5:
        raise ValueError("equipped_slot_index must be within [1, 5]")
    for field in ("is_discarded", "is_equipped", "material_source_selected"):
        if field in payload:
            _optional_bool(payload.get(field), default=False)
    _optional_nonnegative_int(payload.get("material_source_level"), "material_source_level")
    locked = _optional_nonnegative_int(
        payload.get("locked_substat_slots"),
        "locked_substat_slots",
    )
    if locked is not None and locked > 5:
        raise ValueError("locked_substat_slots must be within [0, 5]")


def _parse_correction_pointer(field_path: str) -> tuple[str, ...]:
    if not field_path.startswith("/"):
        raise ValueError("field_path must be an RFC 6901 JSON pointer")
    parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in field_path[1:].split("/"))
    if not parts or not parts[0] or parts[0] == "_ingest":
        raise ValueError("field_path is not correctable")
    if len(parts) == 1 and parts[0] in _CORRECTABLE_SCALARS:
        return parts
    if (
        len(parts) == 3
        and parts[0] in _STAT_COLLECTIONS
        and (parts[1].isdigit() or parts[1] == "-")
        and parts[2] in _STAT_FIELDS
        and parts[1] != "-"
    ):
        return parts
    if len(parts) == 2 and parts[0] in _STAT_COLLECTIONS and parts[1] == "-":
        return parts
    raise ValueError("field_path is not a supported correctable field")


def _apply_correction(
    payload: dict[str, Any],
    pointer: tuple[str, ...],
    new_value: Any,
) -> Any:
    if len(pointer) == 1:
        key = pointer[0]
        if key not in payload:
            raise ValueError(f"field does not exist: /{key}")
        old = deepcopy(payload[key])
        payload[key] = deepcopy(new_value)
        return old
    collection_name = pointer[0]
    collection = payload.get(collection_name)
    if not isinstance(collection, list):
        raise ValueError(f"field is not a list: /{collection_name}")
    if pointer[1] == "-":
        stat = _json_object(new_value, label="new stat")
        _validate_stat(stat, f"{collection_name}[-]")
        collection.append(stat)
        return None
    index = int(pointer[1])
    if index >= len(collection) or not isinstance(collection[index], dict):
        raise ValueError(f"stat index does not exist: /{collection_name}/{index}")
    field = pointer[2]
    if field not in collection[index]:
        raise ValueError(f"stat field does not exist: /{collection_name}/{index}/{field}")
    old = deepcopy(collection[index][field])
    collection[index][field] = deepcopy(new_value)
    return old


def _page_type(value: object) -> str:
    text = _required_text(value, "page_type", maximum=60)
    if text not in {item.value for item in PageType}:
        raise ValueError(f"unsupported page type: {text}")
    return text


def _confidence_map(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("confidence must be an object")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("confidence keys must be strings")
        if item is None:
            continue
        confidence = _optional_confidence(item, f"confidence.{key}")
        if confidence is not None:
            result[key] = confidence
    return result


def _optional_confidence(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric or null")
    normalized = float(value)
    if not isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{label} must be within [0, 1]")
    return normalized


def _stat_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        stat = _json_object(item, label=f"{label}[{index}]")
        _validate_stat(stat, f"{label}[{index}]")
        result.append(stat)
    return result


def _validate_stat(stat: Mapping[str, Any], label: str) -> None:
    unknown = set(stat).difference(_STAT_FIELDS)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(sorted(unknown))}")
    for field in ("name", "unit", "raw_name", "raw_value", "stat_kind"):
        _optional_text(stat.get(field), f"{label}.{field}", maximum=160)
    stat_kind = stat.get("stat_kind")
    if stat_kind is not None and stat_kind not in {item.value for item in StatKind}:
        raise ValueError(f"{label}.stat_kind is unsupported: {stat_kind}")
    value = stat.get("value")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label}.value must be numeric or null")
        if not isfinite(float(value)):
            raise ValueError(f"{label}.value must be finite")
    _optional_nonnegative_int(stat.get("slot_index"), f"{label}.slot_index")
    for field in ("confidence", "name_confidence", "value_confidence"):
        _optional_confidence(stat.get(field), f"{label}.{field}")


def _optional_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("boolean session fields must be true, false, or null")
    return value


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer or null")
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    result = _optional_nonnegative_int(value, label)
    if result == 0:
        raise ValueError(f"{label} must be positive or null")
    return result


def _required_text(value: object, label: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return normalized


def _optional_text(value: object, label: str, *, maximum: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    normalized = value.strip()
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return normalized or None


def _json_object(
    value: Mapping[str, object] | object,
    *,
    label: str,
    reject_images: bool = False,
) -> dict[str, Any]:
    normalized = _json_value(value, label=label, reject_images=reject_images)
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} must be a JSON object")
    return normalized


def _optional_json_object(value: object, *, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_object(value, label=label, reject_images=True)


def _json_list_of_objects(value: object, *, label: str) -> list[dict[str, Any]]:
    normalized = _json_value(value, label=label, reject_images=True)
    if not isinstance(normalized, list):
        raise ValueError(f"{label} must be a JSON list")
    if not all(isinstance(item, dict) for item in normalized):
        raise ValueError(f"{label} entries must be JSON objects")
    return normalized


def _json_value(value: object, *, label: str, reject_images: bool = False) -> Any:
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(rendered)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON data") from exc
    if reject_images:
        _reject_image_payload(normalized, path=label)
    return normalized


def _unwrap_live_result(
    payload: dict[str, Any],
    *,
    source_sequence: int | None,
    occurred_at: datetime | None,
) -> tuple[dict[str, Any], int | None, datetime | None]:
    """Accept an observation, AnalysisResult mapping, or LiveRecognitionResult mapping."""

    analysis = payload.get("analysis")
    if isinstance(analysis, dict):
        if source_sequence is None:
            sequence_value = payload.get("source_sequence")
            if isinstance(sequence_value, int) and not isinstance(sequence_value, bool):
                source_sequence = sequence_value
        if occurred_at is None:
            occurred_at = _parse_optional_timestamp(payload.get("captured_at"))
        payload = analysis

    observation = payload.get("observation")
    if isinstance(observation, dict) and (
        "relevant" in payload or "request_content" in payload or "confidence" in payload
    ):
        payload = observation
    return payload, source_sequence, occurred_at


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = _required_text(value, "captured_at", maximum=80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("captured_at must be an ISO 8601 timestamp") from exc
    return _as_utc(parsed)


def _reject_image_payload(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in _IMAGE_PAYLOAD_KEYS:
                raise ValueError(f"{path}.{key} is image data and cannot be persisted")
            _reject_image_payload(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        if _looks_like_pixel_matrix(value):
            raise ValueError(f"{path} appears to contain image data and cannot be persisted")
        for index, item in enumerate(value):
            _reject_image_payload(item, path=f"{path}[{index}]")


def _looks_like_pixel_matrix(value: list[Any]) -> bool:
    """Detect dense 2-D/3-D numeric arrays even when disguised under a generic key."""

    if len(value) < 2:
        return False
    sample = value[: min(len(value), 4)]
    return all(_looks_like_pixel_row(row) for row in sample)


def _looks_like_pixel_row(value: Any) -> bool:
    if not isinstance(value, list) or len(value) < 8:
        return False
    sample = value[: min(len(value), 16)]
    if all(_is_json_number(item) for item in sample):
        return True
    return all(
        isinstance(item, list)
        and len(item) in {3, 4}
        and all(_is_json_number(channel) for channel in item)
        for item in sample
    )


def _is_json_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get_optional_fk(
    session: Session,
    record_type: type[Any],
    record_id: str | None,
    label: str,
) -> Any | None:
    if record_id is None:
        return None
    record = session.get(record_type, record_id)
    if record is None:
        raise LookupError(f"{label} not found: {record_id}")
    return record


def _require_record(
    session: Session,
    record_type: type[Any],
    record_id: str | None,
    label: str,
) -> Any:
    if record_id is None:
        raise LookupError(f"{label} is not linked")
    record = session.get(record_type, record_id)
    if record is None:
        raise LookupError(f"{label} not found: {record_id}")
    return record


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _capture_snapshot(record: CaptureSessionRecord) -> CaptureSessionSnapshot:
    return CaptureSessionSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        backend_kind=record.backend_kind,
        monitor_index=record.monitor_index,
        sampling_hz=record.sampling_hz,
        stable_wait_ms=record.stable_wait_ms,
        status=record.status,
        started_at=_iso(record.started_at) or "",
        stopped_at=_iso(record.stopped_at),
        assigned_character_id=record.assigned_character_id,
        scoring_profile_id=record.scoring_profile_id,
        error_summary=record.error_summary,
    )


def _session_summary(session: Session, record: EchoSessionRecord) -> EchoSessionSummary:
    return _session_summaries(session, (record,))[0]


def _session_summaries(
    session: Session, records: Sequence[EchoSessionRecord]
) -> tuple[EchoSessionSummary, ...]:
    """Batch summary fields without hydrating observation payloads.

    Bounded IN clauses also work with SQLite builds having the older 999 bind
    limit. sequence_no is unique within a session and defines history order.
    """
    summaries: dict[str, EchoSessionSummary] = {}
    for start in range(0, len(records), 400):
        identifiers = [record.id for record in records[start : start + 400]]
        observations = (
            select(
                EchoObservationRecord.echo_session_id.label("session_id"),
                func.count(EchoObservationRecord.id).label("observation_count"),
                func.max(EchoObservationRecord.sequence_no).label("latest_sequence"),
            )
            .where(EchoObservationRecord.echo_session_id.in_(identifiers))
            .group_by(EchoObservationRecord.echo_session_id)
            .subquery()
        )
        statement = (
            select(
                EchoSessionRecord,
                EchoRecord,
                observations.c.observation_count,
                EchoObservationRecord.id,
                EchoObservationRecord.requires_review,
            )
            .outerjoin(EchoRecord, EchoRecord.id == EchoSessionRecord.echo_id)
            .outerjoin(observations, observations.c.session_id == EchoSessionRecord.id)
            .outerjoin(
                EchoObservationRecord,
                (EchoObservationRecord.echo_session_id == EchoSessionRecord.id)
                & (EchoObservationRecord.sequence_no == observations.c.latest_sequence),
            )
            .where(EchoSessionRecord.id.in_(identifiers))
        )
        # The outer joins make the summary columns nullable. Declare that SQL
        # boundary explicitly across SQLAlchemy 2.0/2.1 Row typing differences.
        summary_rows = cast(
            Iterable[
                tuple[EchoSessionRecord, EchoRecord | None, int | None, str | None, bool | None]
            ],
            session.execute(statement),
        )
        for record, echo, count, latest_id, review in summary_rows:
            summaries[record.id] = _summary_fields(
                record, echo, int(count or 0), latest_id, bool(review)
            )
    return tuple(summaries[record.id] for record in records)


def _summary_fields(
    record: EchoSessionRecord,
    echo: EchoRecord | None,
    observation_count: int,
    latest_id: str | None,
    requires_review: bool,
) -> EchoSessionSummary:
    return EchoSessionSummary(
        id=record.id,
        capture_session_id=record.capture_session_id,
        local_session_id=record.local_session_id,
        current_state=record.current_state,
        is_discarded=record.is_discarded,
        was_consumed=record.was_consumed,
        consumed_into_session_id=record.consumed_into_session_id,
        started_at=_iso(record.started_at) or "",
        ended_at=_iso(record.ended_at),
        echo_id=record.echo_id,
        name=echo.name if echo is not None else None,
        level=echo.level if echo is not None else None,
        cost=echo.cost if echo is not None else None,
        final_status=echo.final_status if echo is not None else EchoDisposition.UNDECIDED.value,
        score=echo.score if echo is not None else None,
        character_id=echo.character_id if echo is not None else None,
        slot_index=echo.slot_index if echo is not None else None,
        observation_count=observation_count,
        latest_observation_id=latest_id,
        requires_review=requires_review,
    )


def _observation_snapshot(record: EchoObservationRecord) -> ObservationSnapshot:
    return ObservationSnapshot(
        id=record.id,
        sequence_no=record.sequence_no,
        observed_at=_iso(record.observed_at) or "",
        requires_review=record.requires_review,
        raw_payload=deepcopy(record.raw_payload_json),
        effective_payload=deepcopy(record.effective_payload_json),
    )


def _event_snapshot(record: EchoEventRecord) -> EchoEventSnapshot:
    return EchoEventSnapshot(
        id=record.id,
        sequence_no=record.sequence_no,
        event_type=record.event_type,
        echo_session_id=record.echo_session_id,
        related_session_id=record.related_session_id,
        confidence=record.confidence,
        notes=tuple(record.notes_json),
        payload=deepcopy(record.payload_json),
        occurred_at=_iso(record.occurred_at) or "",
    )


def _correction_snapshot(record: ObservationCorrectionRecord) -> CorrectionSnapshot:
    return CorrectionSnapshot(
        id=record.id,
        observation_id=record.observation_id,
        field_path=record.field_path,
        old_value=deepcopy(record.old_value_json),
        new_value=deepcopy(record.new_value_json),
        reason=record.reason,
        actor=record.actor,
        corrected_at=_iso(record.corrected_at) or "",
    )


__all__ = [
    "CaptureSessionSnapshot",
    "CorrectionSnapshot",
    "EchoDisposition",
    "EchoEventSnapshot",
    "EchoHistory",
    "EchoHistoryService",
    "EchoSessionSummary",
    "IngestReceipt",
    "ObservationSnapshot",
]
