"""Immutable, versioned recurrence values.

Unknown versions and shapes are deliberately preserved as opaque JSON.  They
must never be interpreted as a familiar schedule because doing so could move a
user's reminder to the wrong day.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

RECURRENCE_SCHEMA_VERSION = 1
_KNOWN_KEYS = frozenset(
    {"schema_version", "frequency", "interval", "series_id", "occurrence_index"}
)


class RecurrenceFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    frequency: RecurrenceFrequency
    interval: int = 1
    series_id: str = ""
    occurrence_index: int = 0
    schema_version: int = RECURRENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RECURRENCE_SCHEMA_VERSION:
            raise ValueError("recurrence schema version is not supported")
        if not isinstance(self.frequency, RecurrenceFrequency):
            raise ValueError("recurrence frequency must be daily or weekly")
        if isinstance(self.interval, bool) or not 1 <= self.interval <= 365:
            raise ValueError("recurrence interval must be within 1..365")
        if not isinstance(self.series_id, str):
            raise ValueError("recurrence series_id must be text")
        if isinstance(self.occurrence_index, bool) or self.occurrence_index < 0:
            raise ValueError("recurrence occurrence_index must be nonnegative")

    @classmethod
    def new(cls, frequency: RecurrenceFrequency | str, interval: int = 1) -> RecurrenceRule:
        try:
            safe_frequency = RecurrenceFrequency(frequency)
        except (TypeError, ValueError) as exc:
            raise ValueError("recurrence frequency must be daily or weekly") from exc
        return cls(frequency=safe_frequency, interval=interval, series_id=str(uuid4()))

    def next_index(self) -> RecurrenceRule:
        return RecurrenceRule(
            frequency=self.frequency,
            interval=self.interval,
            series_id=self.series_id,
            occurrence_index=self.occurrence_index + 1,
        )


@dataclass(frozen=True, slots=True)
class UnknownRecurrenceRule:
    """Opaque payload that the installed application version cannot confirm."""

    payload_json: str
    reason: str

    def to_json_dict(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("opaque recurrence payload is not a JSON object")
        return value


class UnknownRecurrenceRuleError(ValueError):
    """Raised when an action would require guessing an unknown recurrence."""


def recurrence_payload(rule: RecurrenceRule) -> dict[str, object]:
    """Return a new JSON-safe payload so callers cannot mutate the rule."""

    if not isinstance(rule, RecurrenceRule):
        raise ValueError("rule must be RecurrenceRule")
    series_id = rule.series_id or str(uuid4())
    return {
        "schema_version": RECURRENCE_SCHEMA_VERSION,
        "frequency": rule.frequency.value,
        "interval": rule.interval,
        "series_id": series_id,
        "occurrence_index": rule.occurrence_index,
    }


def parse_recurrence_payload(
    payload: Mapping[str, object] | None,
) -> RecurrenceRule | UnknownRecurrenceRule | None:
    """Parse known v1 payloads and preserve every other shape without guessing."""

    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return _unknown(payload, "recurrence payload is not an object")
    raw = dict(payload)
    if frozenset(raw) != _KNOWN_KEYS:
        return _unknown(raw, "recurrence payload has an unknown shape")
    if raw.get("schema_version") != RECURRENCE_SCHEMA_VERSION:
        return _unknown(raw, "recurrence schema version is not supported")
    frequency_value = raw.get("frequency")
    try:
        if not isinstance(frequency_value, str):
            raise ValueError
        frequency = RecurrenceFrequency(frequency_value)
    except (TypeError, ValueError):
        return _unknown(raw, "recurrence frequency is not supported")
    interval = raw.get("interval")
    series_id = raw.get("series_id")
    occurrence_index = raw.get("occurrence_index")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 1 <= interval <= 365
        or not isinstance(series_id, str)
        or not series_id.strip()
        or isinstance(occurrence_index, bool)
        or not isinstance(occurrence_index, int)
        or occurrence_index < 0
    ):
        return _unknown(raw, "recurrence payload contains invalid values")
    return RecurrenceRule(
        frequency=frequency,
        interval=interval,
        series_id=series_id,
        occurrence_index=occurrence_index,
    )


def _unknown(payload: object, reason: str) -> UnknownRecurrenceRule:
    try:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        payload_json = json.dumps({"unreadable_payload_type": type(payload).__name__})
    return UnknownRecurrenceRule(payload_json=payload_json, reason=reason)
