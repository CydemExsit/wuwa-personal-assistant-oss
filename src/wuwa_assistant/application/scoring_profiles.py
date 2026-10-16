"""Dataset-scoped persistence for editable scoring profiles.

System template contents deliberately live outside this module.  The scoring
package supplies versioned, JSON-safe definitions to :meth:`ensure_system_templates`;
this service only validates and persists them.  ORM records never escape the
application boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from wuwa_assistant.scoring import StatKey, unit_for_stat
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    AppSettingRecord,
    DataSetRecord,
    ScoringProfileRecord,
    StatWeightRecord,
    UpgradeDecisionRecord,
    utc_now,
)

type JsonObject = dict[str, Any]
type ProfileDefinitionLike = ScoringProfileDefinition | Mapping[str, object]
type StatWeightDefinitionLike = StatWeightDefinition | Mapping[str, object]
type SystemTemplateDefinitionLike = SystemTemplateDefinition | Mapping[str, object]

_MISSING = object()
_DEFAULT_SETTING_PREFIX = "scoring/default_profile/"


class ProfileInUseError(RuntimeError):
    """Raised when deleting a profile would invalidate historical decisions."""


@dataclass(frozen=True, slots=True)
class ScoreThresholds:
    """User-editable 0-100 score bands in ascending order."""

    usable: float = 55.0
    passing: float = 70.0
    perfect: float = 88.0
    exceptional: float = 95.0

    def __post_init__(self) -> None:
        values = (self.usable, self.passing, self.perfect, self.exceptional)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
            for value in values
        ):
            raise ValueError("score thresholds must be finite numbers")
        if any(not 0.0 <= value <= 100.0 for value in values):
            raise ValueError("score thresholds must be within [0, 100]")
        if list(values) != sorted(values):
            raise ValueError(
                "score thresholds must be ordered usable <= passing <= perfect <= exceptional"
            )

    def to_json_dict(self) -> dict[str, float]:
        return {
            "usable": float(self.usable),
            "passing": float(self.passing),
            "perfect": float(self.perfect),
            "exceptional": float(self.exceptional),
        }


@dataclass(frozen=True, slots=True)
class StatWeightDefinition:
    """One base or conditional weight row accepted by the storage service."""

    stat_name: str
    weight: float
    unit: str = ""
    cap_value: float | None = None
    condition_key: str = "always"
    condition: JsonObject | None = None
    display_order: int = 0


@dataclass(frozen=True, slots=True)
class ScoringProfileDefinition:
    """Editable profile fields independent of persistence identity."""

    name: str
    template_kind: str = "custom"
    description: str | None = None
    thresholds: ScoreThresholds = ScoreThresholds()
    threshold_rules: tuple[JsonObject, ...] = ()
    crit_balance_rule: JsonObject | None = None
    roll_data_version: str | None = None


@dataclass(frozen=True, slots=True)
class SystemTemplateDefinition:
    """Versioned seed supplied by the scoring package, not hard-coded here."""

    profile: ScoringProfileDefinition
    weights: tuple[StatWeightDefinition, ...]
    version: int
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class StatWeightSnapshot:
    id: str
    stat_name: str
    unit: str
    weight: float
    cap_value: float | None
    condition_key: str
    condition: JsonObject | None
    display_order: int

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "stat_name": self.stat_name,
            "unit": self.unit,
            "weight": self.weight,
            "cap_value": self.cap_value,
            "condition_key": self.condition_key,
            "condition": deepcopy(self.condition),
            "display_order": self.display_order,
        }


@dataclass(frozen=True, slots=True)
class ScoringProfileSnapshot:
    id: str
    data_set_id: str | None
    name: str
    template_kind: str
    description: str | None
    thresholds: ScoreThresholds
    threshold_rules: tuple[JsonObject, ...]
    crit_balance_rule: JsonObject | None
    roll_data_version: str | None
    is_system_template: bool
    is_default: bool
    version: int
    weights: tuple[StatWeightSnapshot, ...]

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "name": self.name,
            "template_kind": self.template_kind,
            "description": self.description,
            "thresholds": self.thresholds.to_json_dict(),
            "threshold_rules": deepcopy(list(self.threshold_rules)),
            "crit_balance_rule": deepcopy(self.crit_balance_rule),
            "roll_data_version": self.roll_data_version,
            "is_system_template": self.is_system_template,
            "is_default": self.is_default,
            "version": self.version,
            "weights": [weight.to_json_dict() for weight in self.weights],
        }


class ScoringProfileService:
    """Short-transaction application boundary for scoring configuration."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def ensure_system_templates(
        self,
        definitions: Iterable[SystemTemplateDefinitionLike],
    ) -> tuple[ScoringProfileSnapshot, ...]:
        """Create or upgrade global read-only templates idempotently.

        A seed is changed only when its incoming version is newer.  Passing the
        same version again therefore cannot silently rewrite a released
        template.  Extra keys on mapping definitions are intentionally ignored
        so source/provenance metadata may remain in versioned scoring data files.
        """

        normalized = tuple(_coerce_system_template(item) for item in definitions)
        kinds = [item.profile.template_kind for item in normalized]
        names = [item.profile.name for item in normalized]
        if len(kinds) != len(set(kinds)):
            raise ValueError("system template kinds must be unique")
        if len(names) != len(set(names)):
            raise ValueError("system template names must be unique")
        default_kinds = [item.profile.template_kind for item in normalized if item.is_default]
        if len(default_kinds) > 1:
            raise ValueError("at most one system template may be the default")

        with self._session_factory.begin() as session:
            existing = list(
                session.scalars(
                    select(ScoringProfileRecord).where(
                        ScoringProfileRecord.is_system_template.is_(True),
                        ScoringProfileRecord.data_set_id.is_(None),
                    )
                ).all()
            )
            by_kind: dict[str, ScoringProfileRecord] = {}
            by_name: dict[str, ScoringProfileRecord] = {}
            for stored_record in existing:
                if stored_record.template_kind in by_kind:
                    raise RuntimeError(
                        f"duplicate stored system template kind: {stored_record.template_kind}"
                    )
                if stored_record.name in by_name:
                    raise RuntimeError(
                        f"duplicate stored system template name: {stored_record.name}"
                    )
                by_kind[stored_record.template_kind] = stored_record
                by_name[stored_record.name] = stored_record

            selected_records: list[ScoringProfileRecord] = []
            for item in normalized:
                profile = item.profile
                record = by_kind.get(profile.template_kind)
                name_owner = by_name.get(profile.name)
                if record is None and name_owner is not None:
                    raise ValueError(
                        "system template name is already owned by another template kind"
                    )
                if record is None:
                    record = ScoringProfileRecord(
                        data_set_id=None,
                        name=profile.name,
                        template_kind=profile.template_kind,
                        is_system_template=True,
                        is_default=False,
                        version=item.version,
                    )
                    _apply_profile_fields(record, profile)
                    session.add(record)
                    session.flush()
                    _replace_weight_records(session, record.id, item.weights)
                    by_kind[profile.template_kind] = record
                    by_name[profile.name] = record
                elif item.version > record.version:
                    other_name_owner = by_name.get(profile.name)
                    if other_name_owner is not None and other_name_owner.id != record.id:
                        raise ValueError(
                            "system template name is already owned by another template kind"
                        )
                    by_name.pop(record.name, None)
                    _apply_profile_fields(record, profile)
                    record.version = item.version
                    record.updated_at = utc_now()
                    _replace_weight_records(session, record.id, item.weights)
                    by_name[record.name] = record
                selected_records.append(record)

            if default_kinds:
                selected_kind = default_kinds[0]
                for record in existing + selected_records:
                    record.is_default = record.template_kind == selected_kind

            session.flush()
            return tuple(_snapshot(session, record) for record in selected_records)

    def create_user_profile(
        self,
        *,
        data_set_id: str,
        definition: ProfileDefinitionLike,
        weights: Iterable[StatWeightDefinitionLike] | None = None,
        make_default: bool = False,
    ) -> ScoringProfileSnapshot:
        profile = _coerce_profile_definition(definition)
        safe_weights = _coerce_weight_collection(
            _mapping_weights(definition) if weights is None else weights
        )
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            _ensure_user_name_available(session, data_set_id, profile.name)
            record = ScoringProfileRecord(
                data_set_id=data_set_id,
                name=profile.name,
                template_kind=profile.template_kind,
                is_system_template=False,
                is_default=False,
                version=1,
            )
            _apply_profile_fields(record, profile)
            session.add(record)
            session.flush()
            _replace_weight_records(session, record.id, safe_weights)
            if make_default:
                _set_data_set_default(session, data_set_id, record)
            session.flush()
            return _data_set_snapshot(session, record, data_set_id)

    def update_user_profile(
        self,
        profile_id: str,
        *,
        data_set_id: str,
        definition: ProfileDefinitionLike,
        weights: Iterable[StatWeightDefinitionLike] | None = None,
        make_default: bool | None = None,
    ) -> ScoringProfileSnapshot:
        """Update metadata and, when supplied, replace weights atomically."""

        profile = _coerce_profile_definition(definition)
        safe_weights = (
            None
            if weights is None and not _mapping_has_weights(definition)
            else _coerce_weight_collection(
                _mapping_weights(definition) if weights is None else weights
            )
        )
        with self._session_factory.begin() as session:
            record = _require_user_profile(session, profile_id, data_set_id)
            _ensure_user_name_available(
                session,
                data_set_id,
                profile.name,
                excluding_profile_id=record.id,
            )
            _apply_profile_fields(record, profile)
            if safe_weights is not None:
                _replace_weight_records(session, record.id, safe_weights)
            if make_default is True:
                _set_data_set_default(session, data_set_id, record)
            elif make_default is False:
                record.is_default = False
                _clear_default_selection_if(session, data_set_id, record.id)
            record.version += 1
            record.updated_at = utc_now()
            session.flush()
            return _data_set_snapshot(session, record, data_set_id)

    def replace_weights(
        self,
        profile_id: str,
        *,
        data_set_id: str,
        weights: Iterable[StatWeightDefinitionLike],
    ) -> ScoringProfileSnapshot:
        """Replace all editable weights in one rollback-safe transaction."""

        safe_weights = _coerce_weight_collection(weights)
        with self._session_factory.begin() as session:
            record = _require_user_profile(session, profile_id, data_set_id)
            _replace_weight_records(session, record.id, safe_weights)
            record.version += 1
            record.updated_at = utc_now()
            session.flush()
            return _data_set_snapshot(session, record, data_set_id)

    def clone_template(
        self,
        source_profile_id: str,
        *,
        data_set_id: str,
        name: str,
        make_default: bool = False,
    ) -> ScoringProfileSnapshot:
        """Clone a visible template/profile into an independently editable copy."""

        normalized_name = _required_text(name, "profile name", maximum=160)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            source = _require_visible_profile(session, source_profile_id, data_set_id)
            _ensure_user_name_available(session, data_set_id, normalized_name)
            record = ScoringProfileRecord(
                data_set_id=data_set_id,
                name=normalized_name,
                template_kind=source.template_kind,
                description=source.description,
                usable_threshold=source.usable_threshold,
                passing_threshold=source.passing_threshold,
                perfect_threshold=source.perfect_threshold,
                exceptional_threshold=source.exceptional_threshold,
                threshold_rules_json=_json_array(source.threshold_rules_json, "threshold rules"),
                crit_balance_rule_json=_optional_json_object(
                    source.crit_balance_rule_json, "crit balance rule"
                ),
                roll_data_version=source.roll_data_version,
                is_system_template=False,
                is_default=False,
                version=1,
            )
            session.add(record)
            session.flush()
            source_weights = tuple(
                _definition_from_record(weight) for weight in _weight_records(session, source.id)
            )
            _replace_weight_records(session, record.id, source_weights)
            if make_default:
                _set_data_set_default(session, data_set_id, record)
            session.flush()
            return _data_set_snapshot(session, record, data_set_id)

    def set_default(self, profile_id: str, *, data_set_id: str) -> ScoringProfileSnapshot:
        """Select a visible user profile or system template for one dataset."""

        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = _require_visible_profile(session, profile_id, data_set_id)
            _set_data_set_default(session, data_set_id, record)
            session.flush()
            return _data_set_snapshot(session, record, data_set_id)

    def get_default(self, *, data_set_id: str) -> ScoringProfileSnapshot | None:
        """Return the dataset default, falling back to the global system default."""

        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            record = _resolve_default_record(session, data_set_id)
            return None if record is None else _snapshot(session, record, is_default=True)

    def get_profile(
        self,
        profile_id: str,
        *,
        data_set_id: str,
    ) -> ScoringProfileSnapshot:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            return _snapshot(
                session,
                _require_visible_profile(session, profile_id, data_set_id),
                is_default=_selected_default_id(session, data_set_id) == profile_id,
            )

    def list_profiles(
        self,
        *,
        data_set_id: str,
        include_system_templates: bool = True,
    ) -> tuple[ScoringProfileSnapshot, ...]:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            scope = ScoringProfileRecord.data_set_id == data_set_id
            if include_system_templates:
                scope = or_(
                    scope,
                    (
                        ScoringProfileRecord.data_set_id.is_(None)
                        & ScoringProfileRecord.is_system_template.is_(True)
                    ),
                )
            records = session.scalars(
                select(ScoringProfileRecord)
                .where(scope)
                .order_by(
                    ScoringProfileRecord.is_system_template.desc(),
                    ScoringProfileRecord.name,
                    ScoringProfileRecord.id,
                )
            ).all()
            selected_id = _selected_default_id(session, data_set_id)
            return tuple(
                _snapshot(session, record, is_default=record.id == selected_id)
                for record in records
            )

    def delete_user_profile(self, profile_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            record = _require_user_profile(session, profile_id, data_set_id)
            decision_id = session.scalar(
                select(UpgradeDecisionRecord.id)
                .where(UpgradeDecisionRecord.scoring_profile_id == record.id)
                .limit(1)
            )
            if decision_id is not None:
                raise ProfileInUseError(
                    "scoring profile has historical upgrade decisions and cannot be deleted"
                )
            _clear_default_selection_if(session, data_set_id, record.id)
            session.delete(record)
            session.flush()
            return True


def _coerce_system_template(value: SystemTemplateDefinitionLike) -> SystemTemplateDefinition:
    if isinstance(value, SystemTemplateDefinition):
        profile = _coerce_profile_definition(value.profile)
        weights = _coerce_weight_collection(value.weights)
        version = _positive_int(value.version, "system template version")
        return SystemTemplateDefinition(profile, weights, version, bool(value.is_default))
    if not isinstance(value, Mapping):
        raise ValueError("system template must be a definition or JSON object")
    profile = _coerce_profile_definition(value)
    weights = _coerce_weight_collection(_mapping_weights(value))
    version = _positive_int(value.get("version", 1), "system template version")
    is_default = _strict_bool(value.get("is_default", False), "is_default")
    return SystemTemplateDefinition(profile, weights, version, is_default)


def _coerce_profile_definition(value: ProfileDefinitionLike) -> ScoringProfileDefinition:
    if isinstance(value, ScoringProfileDefinition):
        return ScoringProfileDefinition(
            name=_required_text(value.name, "profile name", maximum=160),
            template_kind=_required_text(value.template_kind, "template kind", maximum=40),
            description=_optional_text(value.description, "description"),
            thresholds=_coerce_thresholds(value.thresholds),
            threshold_rules=tuple(_json_array(value.threshold_rules, "threshold rules")),
            crit_balance_rule=_optional_json_object(value.crit_balance_rule, "crit balance rule"),
            roll_data_version=_optional_text(
                value.roll_data_version, "roll data version", maximum=80
            ),
        )
    if not isinstance(value, Mapping):
        raise ValueError("profile definition must be a definition or JSON object")
    thresholds_value = value.get("thresholds", _MISSING)
    if thresholds_value is _MISSING:
        thresholds_value = {
            "usable": value.get("usable_threshold", 55.0),
            "passing": value.get("passing_threshold", 70.0),
            "perfect": value.get("perfect_threshold", 88.0),
            "exceptional": value.get("exceptional_threshold", 95.0),
        }
    threshold_rules = value.get("threshold_rules", value.get("threshold_rules_json", []))
    crit_rule = value.get("crit_balance_rule", value.get("crit_balance_rule_json"))
    return ScoringProfileDefinition(
        name=_required_text(value.get("name"), "profile name", maximum=160),
        template_kind=_required_text(
            value.get("template_kind", "custom"), "template kind", maximum=40
        ),
        description=_optional_text(value.get("description"), "description"),
        thresholds=_coerce_thresholds(thresholds_value),
        threshold_rules=tuple(_json_array(threshold_rules, "threshold rules")),
        crit_balance_rule=_optional_json_object(crit_rule, "crit balance rule"),
        roll_data_version=_optional_text(
            value.get("roll_data_version"), "roll data version", maximum=80
        ),
    )


def _coerce_thresholds(value: object) -> ScoreThresholds:
    if isinstance(value, ScoreThresholds):
        return ScoreThresholds(
            float(value.usable),
            float(value.passing),
            float(value.perfect),
            float(value.exceptional),
        )
    if not isinstance(value, Mapping):
        raise ValueError("thresholds must be a JSON object")
    return ScoreThresholds(
        _finite_float(value.get("usable", value.get("usable_threshold", 55.0)), "usable"),
        _finite_float(value.get("passing", value.get("passing_threshold", 70.0)), "passing"),
        _finite_float(value.get("perfect", value.get("perfect_threshold", 88.0)), "perfect"),
        _finite_float(
            value.get("exceptional", value.get("exceptional_threshold", 95.0)),
            "exceptional",
        ),
    )


def _coerce_weight_collection(
    values: Iterable[StatWeightDefinitionLike],
) -> tuple[StatWeightDefinition, ...]:
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError("weights must be a JSON array")
    normalized = tuple(_coerce_weight(value, index) for index, value in enumerate(values))
    keys = [(item.stat_name, item.unit, item.condition_key) for item in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("weight rules must be unique by stat name, unit, and condition key")
    _validate_scoring_weights(normalized)
    return normalized


def _validate_scoring_weights(definitions: Sequence[StatWeightDefinition]) -> None:
    base_by_stat: dict[tuple[str, str], StatWeightDefinition] = {}
    has_positive_base = False
    for item in definitions:
        try:
            stat = StatKey(item.stat_name)
        except ValueError as exc:
            raise ValueError(f"unknown canonical stat name: {item.stat_name}") from exc
        expected_unit = unit_for_stat(stat)
        if item.unit != expected_unit:
            raise ValueError(
                f"unit {item.unit!r} is invalid for {item.stat_name}; expected {expected_unit!r}"
            )
        if item.cap_value is not None and item.cap_value <= 0:
            raise ValueError("cap value must be positive")
        if item.condition_key == "always":
            base_by_stat[(item.stat_name, item.unit)] = item
            has_positive_base = has_positive_base or item.weight > 0

    if not has_positive_base:
        raise ValueError("scoring profile must contain at least one positive base weight")
    for item in definitions:
        if item.condition_key == "always":
            continue
        base = base_by_stat.get((item.stat_name, item.unit))
        if base is None or base.weight <= 0:
            raise ValueError(
                f"conditional weight requires a positive base weight: {item.stat_name}"
            )


def _coerce_weight(value: StatWeightDefinitionLike, default_order: int) -> StatWeightDefinition:
    stat_name: object
    weight: object
    unit: object
    cap_value: object
    condition_key: object
    condition: object
    display_order: object
    if isinstance(value, StatWeightDefinition):
        stat_name = value.stat_name
        weight = value.weight
        unit = value.unit
        cap_value = value.cap_value
        condition_key = value.condition_key
        condition = value.condition
        display_order = value.display_order
    elif isinstance(value, Mapping):
        stat_name = value.get("stat_name")
        weight = value.get("weight")
        unit = value.get("unit", "")
        cap_value = value.get("cap_value")
        condition_key = value.get("condition_key") or "always"
        condition = value.get("condition", value.get("condition_json"))
        display_order = value.get("display_order", default_order)
    else:
        raise ValueError("weight must be a definition or JSON object")

    normalized_weight = _finite_float(weight, "weight")
    if normalized_weight < 0:
        raise ValueError("weight must be nonnegative")
    normalized_cap = None if cap_value is None else _finite_float(cap_value, "cap value")
    if normalized_cap is not None and normalized_cap < 0:
        raise ValueError("cap value must be nonnegative")
    order = _nonnegative_int(display_order, "display order")
    return StatWeightDefinition(
        stat_name=_required_text(stat_name, "stat name", maximum=120),
        weight=normalized_weight,
        unit=_text(unit, "unit", maximum=16),
        cap_value=normalized_cap,
        condition_key=_required_text(condition_key, "condition key", maximum=120),
        condition=_optional_json_object(condition, "weight condition"),
        display_order=order,
    )


def _mapping_weights(value: object) -> Iterable[StatWeightDefinitionLike]:
    if not isinstance(value, Mapping):
        return ()
    weights = value.get("weights", ())
    if isinstance(weights, (str, bytes, Mapping)) or not isinstance(weights, Iterable):
        raise ValueError("weights must be a JSON array")
    return weights


def _mapping_has_weights(value: object) -> bool:
    return isinstance(value, Mapping) and "weights" in value


def _apply_profile_fields(
    record: ScoringProfileRecord,
    definition: ScoringProfileDefinition,
) -> None:
    record.name = definition.name
    record.template_kind = definition.template_kind
    record.description = definition.description
    record.usable_threshold = definition.thresholds.usable
    record.passing_threshold = definition.thresholds.passing
    record.perfect_threshold = definition.thresholds.perfect
    record.exceptional_threshold = definition.thresholds.exceptional
    record.threshold_rules_json = _json_array(definition.threshold_rules, "threshold rules")
    record.crit_balance_rule_json = _optional_json_object(
        definition.crit_balance_rule, "crit balance rule"
    )
    record.roll_data_version = definition.roll_data_version


def _replace_weight_records(
    session: Session,
    profile_id: str,
    definitions: Sequence[StatWeightDefinition],
) -> None:
    for existing in _weight_records(session, profile_id):
        session.delete(existing)
    session.flush()
    session.add_all(
        [
            StatWeightRecord(
                scoring_profile_id=profile_id,
                stat_name=item.stat_name,
                unit=item.unit,
                weight=item.weight,
                cap_value=item.cap_value,
                condition_key=item.condition_key,
                condition_json=_optional_json_object(item.condition, "weight condition"),
                display_order=item.display_order,
            )
            for item in definitions
        ]
    )
    session.flush()


def _weight_records(session: Session, profile_id: str) -> list[StatWeightRecord]:
    return list(
        session.scalars(
            select(StatWeightRecord)
            .where(StatWeightRecord.scoring_profile_id == profile_id)
            .order_by(
                StatWeightRecord.display_order,
                StatWeightRecord.stat_name,
                StatWeightRecord.unit,
                StatWeightRecord.condition_key,
                StatWeightRecord.id,
            )
        ).all()
    )


def _snapshot(
    session: Session,
    record: ScoringProfileRecord,
    *,
    is_default: bool | None = None,
) -> ScoringProfileSnapshot:
    return ScoringProfileSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        name=record.name,
        template_kind=record.template_kind,
        description=record.description,
        thresholds=ScoreThresholds(
            record.usable_threshold,
            record.passing_threshold,
            record.perfect_threshold,
            record.exceptional_threshold,
        ),
        threshold_rules=tuple(_json_array(record.threshold_rules_json, "threshold rules")),
        crit_balance_rule=_optional_json_object(record.crit_balance_rule_json, "crit balance rule"),
        roll_data_version=record.roll_data_version,
        is_system_template=record.is_system_template,
        is_default=record.is_default if is_default is None else is_default,
        version=record.version,
        weights=tuple(_weight_snapshot(item) for item in _weight_records(session, record.id)),
    )


def _data_set_snapshot(
    session: Session,
    record: ScoringProfileRecord,
    data_set_id: str,
) -> ScoringProfileSnapshot:
    return _snapshot(
        session,
        record,
        is_default=_selected_default_id(session, data_set_id) == record.id,
    )


def _weight_snapshot(record: StatWeightRecord) -> StatWeightSnapshot:
    return StatWeightSnapshot(
        id=record.id,
        stat_name=record.stat_name,
        unit=record.unit,
        weight=record.weight,
        cap_value=record.cap_value,
        condition_key=record.condition_key,
        condition=_optional_json_object(record.condition_json, "weight condition"),
        display_order=record.display_order,
    )


def _definition_from_record(record: StatWeightRecord) -> StatWeightDefinition:
    return StatWeightDefinition(
        stat_name=record.stat_name,
        unit=record.unit,
        weight=record.weight,
        cap_value=record.cap_value,
        condition_key=record.condition_key,
        condition=_optional_json_object(record.condition_json, "weight condition"),
        display_order=record.display_order,
    )


def _require_data_set(session: Session, data_set_id: str) -> DataSetRecord:
    record = session.get(DataSetRecord, data_set_id)
    if record is None:
        raise LookupError(f"data set not found: {data_set_id}")
    return record


def _require_visible_profile(
    session: Session,
    profile_id: str,
    data_set_id: str,
) -> ScoringProfileRecord:
    record = session.get(ScoringProfileRecord, profile_id)
    if record is None or not (
        record.data_set_id == data_set_id
        or (record.data_set_id is None and record.is_system_template)
    ):
        raise LookupError(f"scoring profile not found: {profile_id}")
    return record


def _require_user_profile(
    session: Session,
    profile_id: str,
    data_set_id: str,
) -> ScoringProfileRecord:
    record = _require_visible_profile(session, profile_id, data_set_id)
    if record.is_system_template:
        raise PermissionError("system templates are read-only; clone one before editing")
    if record.data_set_id != data_set_id:
        raise LookupError(f"scoring profile not found: {profile_id}")
    return record


def _ensure_user_name_available(
    session: Session,
    data_set_id: str,
    name: str,
    *,
    excluding_profile_id: str | None = None,
) -> None:
    statement = select(ScoringProfileRecord.id).where(
        ScoringProfileRecord.data_set_id == data_set_id,
        ScoringProfileRecord.name == name,
    )
    if excluding_profile_id is not None:
        statement = statement.where(ScoringProfileRecord.id != excluding_profile_id)
    if session.scalar(statement) is not None:
        raise ValueError(f"a scoring profile named {name!r} already exists in this data set")


def _set_data_set_default(
    session: Session,
    data_set_id: str,
    selected: ScoringProfileRecord,
) -> None:
    records = session.scalars(
        select(ScoringProfileRecord).where(
            ScoringProfileRecord.data_set_id == data_set_id,
            ScoringProfileRecord.is_system_template.is_(False),
        )
    ).all()
    for record in records:
        record.is_default = not selected.is_system_template and record.id == selected.id
    key = _default_setting_key(data_set_id)
    setting = session.get(AppSettingRecord, key)
    if setting is None:
        setting = AppSettingRecord(
            key=key,
            value_json={"profile_id": selected.id},
            value_schema_version=1,
        )
        session.add(setting)
    else:
        setting.value_json = {"profile_id": selected.id}
        setting.value_schema_version = 1
        setting.updated_at = utc_now()


def _resolve_default_record(
    session: Session,
    data_set_id: str,
) -> ScoringProfileRecord | None:
    setting = session.get(AppSettingRecord, _default_setting_key(data_set_id))
    if setting is not None and isinstance(setting.value_json, dict):
        selected_id = setting.value_json.get("profile_id")
        if isinstance(selected_id, str):
            selected = session.get(ScoringProfileRecord, selected_id)
            if selected is not None and (
                selected.data_set_id == data_set_id
                or (selected.data_set_id is None and selected.is_system_template)
            ):
                return selected

    local = session.scalar(
        select(ScoringProfileRecord)
        .where(
            ScoringProfileRecord.data_set_id == data_set_id,
            ScoringProfileRecord.is_system_template.is_(False),
            ScoringProfileRecord.is_default.is_(True),
        )
        .order_by(ScoringProfileRecord.updated_at.desc(), ScoringProfileRecord.id)
    )
    if local is not None:
        return local
    return session.scalar(
        select(ScoringProfileRecord)
        .where(
            ScoringProfileRecord.data_set_id.is_(None),
            ScoringProfileRecord.is_system_template.is_(True),
            ScoringProfileRecord.is_default.is_(True),
        )
        .order_by(ScoringProfileRecord.name, ScoringProfileRecord.id)
    )


def _selected_default_id(session: Session, data_set_id: str) -> str | None:
    record = _resolve_default_record(session, data_set_id)
    return None if record is None else record.id


def _clear_default_selection_if(
    session: Session,
    data_set_id: str,
    profile_id: str,
) -> None:
    setting = session.get(AppSettingRecord, _default_setting_key(data_set_id))
    if (
        setting is not None
        and isinstance(setting.value_json, dict)
        and setting.value_json.get("profile_id") == profile_id
    ):
        session.delete(setting)


def _default_setting_key(data_set_id: str) -> str:
    return f"{_DEFAULT_SETTING_PREFIX}{data_set_id}"


def _json_clone(value: object, label: str) -> Any:
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(rendered)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON data") from exc


def _json_array(value: object, label: str) -> list[JsonObject]:
    safe = _json_clone(value, label)
    if not isinstance(safe, list) or any(not isinstance(item, dict) for item in safe):
        raise ValueError(f"{label} must be an array of JSON objects")
    return safe


def _optional_json_object(value: object, label: str) -> JsonObject | None:
    if value is None:
        return None
    safe = _json_clone(value, label)
    if not isinstance(safe, dict):
        raise ValueError(f"{label} must be a JSON object or null")
    return safe


def _required_text(value: object, label: str, *, maximum: int) -> str:
    normalized = _text(value, label, maximum=maximum).strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _optional_text(value: object, label: str, *, maximum: int | None = None) -> str | None:
    if value is None:
        return None
    normalized = _text(value, label, maximum=maximum).strip()
    return normalized or None


def _text(value: object, label: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return value


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{label} must be a finite number")
    return normalized


def _positive_int(value: object, label: str) -> int:
    normalized = _nonnegative_int(value, label)
    if normalized < 1:
        raise ValueError(f"{label} must be positive")
    return normalized


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must be nonnegative")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


__all__ = [
    "ProfileInUseError",
    "ScoreThresholds",
    "ScoringProfileDefinition",
    "ScoringProfileService",
    "ScoringProfileSnapshot",
    "StatWeightDefinition",
    "StatWeightSnapshot",
    "SystemTemplateDefinition",
]
