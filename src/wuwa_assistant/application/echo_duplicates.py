"""Reversible logical rescan groups; physical Echo and temporal rows stay intact.

This policy implements the user's explicit choice to treat fully matching rolled
stats as likely rescans. It is not a game-provided identity and is never used by
the temporal tracker to infer consumption, upgrade continuity, or missing slots.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wuwa_assistant.models.names import canonical_character_name, character_name_key
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CharacterRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
)

_METADATA_KEY = "echo_duplicate_group_v1"
_POLICY = "exact_complete_numeric_state_v1"


@dataclass(frozen=True, slots=True)
class EchoDuplicateGroup:
    representative_echo_id: str
    member_echo_ids: tuple[str, ...]
    owner_character_id: str | None
    slot_index: int | None
    set_name: str | None
    signature: str | None
    reason: str

    @property
    def rescan_count(self) -> int:
        return len(self.member_echo_ids) - 1


@dataclass(frozen=True, slots=True)
class EchoDuplicatePreview:
    data_set_id: str
    groups: tuple[EchoDuplicateGroup, ...]
    excluded_echo_ids: tuple[str, ...]

    @property
    def physical_count(self) -> int:
        return sum(len(group.member_echo_ids) for group in self.groups)

    @property
    def logical_count(self) -> int:
        return len(self.groups)

    @property
    def rescan_count(self) -> int:
        return self.physical_count - self.logical_count


@dataclass(frozen=True, slots=True)
class EchoDuplicateResult:
    preview: EchoDuplicatePreview
    updated_session_count: int


def _number_key(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return format(number.normalize(), "f") if number.is_finite() else None


def _stat_key(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping):
        return None
    name, unit = value.get("name"), value.get("unit")
    if (
        not isinstance(name, str)
        or not name.strip()
        or (unit is not None and not isinstance(unit, str))
        or unit not in {None, "", "%"}
    ):
        return None
    number = _number_key(value.get("value"))
    if number is None:
        return None
    return canonical_character_name(name), number, unit or ""


def complete_echo_signature(
    *,
    name: str | None,
    level: int | None,
    cost: int | None,
    main_stats: Sequence[object],
    sub_stats: Sequence[object],
) -> str | None:
    """Hash every known numeric field; never group unrolled or incomplete Echoes.

    Primary/fixed rows have distinct roles. Rolled substats are a multiset so a
    display-order change does not manufacture a second item. Numeric equality is
    exact after decimal normalization; no rounding, fuzzy matching, or weighting.
    Set compatibility and manual assignment conflicts are checked separately.
    """

    if (
        not isinstance(name, str)
        or not name.strip()
        or type(level) is not int
        or not 5 <= level <= 25
        or type(cost) is not int
        or cost not in {1, 3, 4}
        or not isinstance(main_stats, (list, tuple))
        or not isinstance(sub_stats, (list, tuple))
        or len(main_stats) != 2
        or len(sub_stats) != level // 5
    ):
        return None
    main: dict[str, tuple[str, str, str]] = {}
    for index, value in enumerate(main_stats):
        key = _stat_key(value)
        if key is None or not isinstance(value, Mapping):
            return None
        role = value.get("stat_kind", ("primary", "fixed")[index])
        if not isinstance(role, str) or role not in {"primary", "fixed"} or role in main:
            return None
        main[role] = key
    subs: list[tuple[str, str, str]] = []
    for value in sub_stats:
        key = _stat_key(value)
        if key is None:
            return None
        subs.append(key)
    payload = (
        canonical_character_name(name),
        level,
        cost,
        main["primary"],
        main["fixed"],
        sorted(subs),
    )
    return sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _signature(record: EchoRecord) -> str | None:
    return complete_echo_signature(
        name=record.name,
        level=record.level,
        cost=record.cost,
        main_stats=record.main_stats_json,
        sub_stats=record.sub_stats_json,
    )


def _time_key(value: datetime | None) -> str:
    if value is None:
        return ""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class _Candidate:
    record: EchoRecord
    signature: str | None
    set_name: str | None
    manual_slot: bool
    latest_at: str
    slot_evidence_at: str


def _strong(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0.90 <= value <= 1.0


def _load_candidates(
    session: Session, data_set_id: str
) -> tuple[list[_Candidate], tuple[str, ...]]:
    records = list(
        session.scalars(
            select(EchoRecord).where(EchoRecord.data_set_id == data_set_id).order_by(EchoRecord.id)
        )
    )
    sessions_by_echo: dict[str, list[EchoSessionRecord]] = defaultdict(list)
    for item in session.scalars(
        select(EchoSessionRecord)
        .join(EchoRecord, EchoSessionRecord.echo_id == EchoRecord.id)
        .where(EchoRecord.data_set_id == data_set_id)
    ):
        if item.echo_id is not None:
            sessions_by_echo[item.echo_id].append(item)
    latest_times = {
        echo_id: _time_key(observed_at)
        for echo_id, observed_at in session.execute(
            select(EchoSessionRecord.echo_id, func.max(EchoObservationRecord.observed_at))
            .join(
                EchoObservationRecord, EchoObservationRecord.echo_session_id == EchoSessionRecord.id
            )
            .join(EchoRecord, EchoSessionRecord.echo_id == EchoRecord.id)
            .where(EchoRecord.data_set_id == data_set_id)
            .group_by(EchoSessionRecord.echo_id)
        )
    }
    character_keys = {
        character.id: character_name_key(character.name)
        for character in session.scalars(
            select(CharacterRecord).where(CharacterRecord.data_set_id == data_set_id)
        )
    }
    by_id = {record.id: record for record in records}
    slot_times: dict[str, str] = {}
    # Only assigned slots need historical owner evidence. Stream the effective
    # payload, never the large raw OCR/provenance fields or historical ORM rows.
    # Python keeps the strict boolean/confidence checks used by the grouping policy.
    evidence = session.execute(
        select(
            EchoSessionRecord.echo_id,
            EchoObservationRecord.observed_at,
            EchoObservationRecord.effective_payload_json,
        )
        .join(EchoSessionRecord, EchoObservationRecord.echo_session_id == EchoSessionRecord.id)
        .join(EchoRecord, EchoSessionRecord.echo_id == EchoRecord.id)
        .where(
            EchoRecord.data_set_id == data_set_id,
            EchoRecord.character_id.is_not(None),
            EchoRecord.slot_index.is_not(None),
        )
        .execution_options(yield_per=256)
    )
    # SQLAlchemy 2.0 and 2.1 expose different Row tuple typing. The join excludes
    # null echo IDs; these selected columns have the same runtime types in both.
    for echo_id, observed_at, payload in cast(
        Iterable[tuple[str, datetime, dict[str, Any]]], evidence
    ):
        record = by_id[echo_id]
        confidence = payload.get("confidence")
        observed_owner = payload.get("equipped_character_name")
        if (
            payload.get("page_type") == "echo_selection"
            and payload.get("is_equipped") is True
            and payload.get("equipped_slot_index") == record.slot_index
            and isinstance(observed_owner, str)
            and character_name_key(observed_owner) == character_keys.get(record.character_id or "")
            and isinstance(confidence, dict)
            and _strong(confidence.get("equipped_slot_index"))
            and _strong(confidence.get("equipped_character_name"))
        ):
            slot_times[echo_id] = max(slot_times.get(echo_id, ""), _time_key(observed_at))
    candidates: list[_Candidate] = []
    excluded: list[str] = []
    for record in records:
        sessions = sessions_by_echo[record.id]
        if (
            record.archived_at is not None
            or record.final_status == "consumed"
            or any(
                item.was_consumed
                or item.consumed_into_session_id is not None
                or item.current_state == "consumed_ended"
                for item in sessions
            )
        ):
            excluded.append(record.id)
            continue
        recognized = bool(sessions) and all(
            item.metadata_json.get("equipment_assignment_source") == "recognition"
            for item in sessions
        )
        candidates.append(
            _Candidate(
                record=record,
                signature=_signature(record),
                set_name=(canonical_character_name(record.set_name) or None)
                if record.set_name
                else None,
                manual_slot=bool(
                    record.character_id and record.slot_index is not None and not recognized
                ),
                latest_at=latest_times.get(record.id, _time_key(record.created_at)),
                slot_evidence_at=slot_times.get(record.id, ""),
            )
        )
    return candidates, tuple(excluded)


def _representative(candidates: list[_Candidate]) -> _Candidate:
    return max(
        candidates,
        key=lambda item: (
            item.manual_slot,
            bool(item.slot_evidence_at),
            item.slot_evidence_at,
            bool(item.record.character_id and item.record.slot_index is not None),
            bool(item.record.character_id),
            bool(item.set_name),
            item.latest_at,
            item.record.id,
        ),
    )


def _group(candidates: list[_Candidate], reason: str) -> EchoDuplicateGroup:
    selected = _representative(candidates)
    known_sets = {item.set_name for item in candidates if item.set_name}
    return EchoDuplicateGroup(
        representative_echo_id=selected.record.id,
        member_echo_ids=tuple(sorted(item.record.id for item in candidates)),
        owner_character_id=selected.record.character_id,
        slot_index=selected.record.slot_index,
        set_name=next(iter(known_sets)) if len(known_sets) == 1 else None,
        signature=selected.signature,
        reason=reason,
    )


def _manual_compatible_groups(candidates: list[_Candidate]) -> list[EchoDuplicateGroup]:
    claims = {
        (item.record.character_id, item.record.slot_index)
        for item in candidates
        if item.manual_slot
    }
    if len(claims) <= 1:
        return [_group(candidates, "exact_complete_stats" if len(candidates) > 1 else "unique")]
    # Multiple manually equipped positions can represent real identical items.
    # Group only members already assigned to the same explicit position.
    partitions: dict[tuple[str | None, int | None], list[_Candidate]] = defaultdict(list)
    groups: list[EchoDuplicateGroup] = []
    for item in candidates:
        claim = (item.record.character_id, item.record.slot_index)
        if claim in claims:
            partitions[claim].append(item)
        else:
            groups.append(_group([item], "manual_slot_conflict"))
    groups.extend(_group(items, "manual_slot_preserved") for items in partitions.values())
    return groups


def _preview(session: Session, data_set_id: str) -> EchoDuplicatePreview:
    candidates, excluded = _load_candidates(session, data_set_id)
    buckets: dict[str, list[_Candidate]] = defaultdict(list)
    groups: list[EchoDuplicateGroup] = []
    for item in candidates:
        if item.signature is None:
            groups.append(_group([item], "incomplete_or_unrolled"))
        else:
            buckets[item.signature].append(item)
    for items in buckets.values():
        known_sets = {item.set_name for item in items if item.set_name}
        if len(known_sets) <= 1:
            groups.extend(_manual_compatible_groups(items))
            continue
        by_set: dict[str, list[_Candidate]] = defaultdict(list)
        for item in items:
            if item.set_name is None:
                groups.append(_group([item], "ambiguous_legacy_set"))
            else:
                by_set[item.set_name].append(item)
        for subset in by_set.values():
            groups.extend(_manual_compatible_groups(subset))
    return EchoDuplicatePreview(
        data_set_id=data_set_id,
        groups=tuple(sorted(groups, key=lambda group: group.representative_echo_id)),
        excluded_echo_ids=excluded,
    )


def group_echo_records(session: Session, *, data_set_id: str) -> tuple[EchoDuplicateGroup, ...]:
    """Return fresh logical groups for active, non-consumed Echoes, including singletons."""

    return _preview(session, data_set_id).groups


def reconcile_echo_duplicates(session: Session, *, data_set_id: str) -> EchoDuplicateResult:
    """Refresh only this policy's session metadata inside the caller's transaction."""

    session.flush()
    preview = _preview(session, data_set_id)
    annotations: dict[str, dict[str, Any]] = {}
    for group in preview.groups:
        if group.rescan_count:
            annotation: dict[str, Any] = {
                "policy": _POLICY,
                "representative_echo_id": group.representative_echo_id,
                "member_echo_ids": list(group.member_echo_ids),
                "signature": group.signature,
            }
            annotations.update({identifier: annotation for identifier in group.member_echo_ids})
    changed = 0
    for item in session.scalars(
        select(EchoSessionRecord)
        .join(EchoRecord, EchoSessionRecord.echo_id == EchoRecord.id)
        .where(EchoRecord.data_set_id == data_set_id)
    ):
        old = item.metadata_json
        updated = dict(old)
        expected_annotation = annotations.get(item.echo_id or "")
        if expected_annotation is None:
            updated.pop(_METADATA_KEY, None)
        else:
            updated[_METADATA_KEY] = expected_annotation
        if old != updated:
            item.metadata_json = updated
            changed += 1
    session.flush()
    return EchoDuplicateResult(preview, changed)


class EchoDuplicateService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def preview(self, data_set_id: str) -> EchoDuplicatePreview:
        with self._session_factory() as session:
            return _preview(session, data_set_id)

    def apply(self, data_set_id: str) -> EchoDuplicateResult:
        with self._session_factory.begin() as session:
            return reconcile_echo_duplicates(session, data_set_id=data_set_id)

    def clear(self, data_set_id: str) -> int:
        """Undo only duplicate annotations; original physical rows never changed."""

        changed = 0
        with self._session_factory.begin() as session:
            for item in session.scalars(
                select(EchoSessionRecord)
                .join(EchoRecord, EchoSessionRecord.echo_id == EchoRecord.id)
                .where(EchoRecord.data_set_id == data_set_id)
            ):
                if _METADATA_KEY in item.metadata_json:
                    updated = dict(item.metadata_json)
                    del updated[_METADATA_KEY]
                    item.metadata_json = updated
                    changed += 1
        return changed
