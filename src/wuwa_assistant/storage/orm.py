"""SQLAlchemy 2 records for the complete V1 local data model.

These records are persistence details.  OCR parsers, state tracking, and Qt
models continue to use domain dataclasses and must not depend on ORM instances.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    and_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class DataSetRecord(IdMixin, Base):
    """A local data partition used only to keep demo and primary data separate."""

    __tablename__ = "data_sets"
    __table_args__ = (
        CheckConstraint("kind IN ('primary', 'demo')", name="kind"),
        UniqueConstraint("kind", "name", name="uq_data_sets_kind_name"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AppSettingRecord(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    value_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ScoringProfileRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "scoring_profiles"
    __table_args__ = (
        CheckConstraint("usable_threshold BETWEEN 0 AND 100", name="usable_score"),
        CheckConstraint("passing_threshold BETWEEN 0 AND 100", name="passing_score"),
        CheckConstraint("perfect_threshold BETWEEN 0 AND 100", name="perfect_score"),
        CheckConstraint("exceptional_threshold BETWEEN 0 AND 100", name="exceptional_score"),
        UniqueConstraint("data_set_id", "name", name="uq_scoring_profiles_dataset_name"),
    )

    data_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    template_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    usable_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=55.0)
    passing_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=70.0)
    perfect_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=88.0)
    exceptional_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=95.0)
    threshold_rules_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    crit_balance_rule_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    roll_data_version: Mapped[str | None] = mapped_column(String(80))
    is_system_template: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CharacterRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "characters"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint("level >= 0", name="level_nonnegative"),
        CheckConstraint("resonance_chain >= 0", name="chain_nonnegative"),
        UniqueConstraint("data_set_id", "name", name="uq_characters_dataset_name"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    element: Mapped[str | None] = mapped_column(String(60))
    role: Mapped[str | None] = mapped_column(String(100))
    is_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resonance_chain: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scoring_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    cultivation_goal_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WeaponRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "weapons"
    __table_args__ = (
        CheckConstraint("level >= 0", name="level_nonnegative"),
        CheckConstraint("resonance_rank >= 0", name="rank_nonnegative"),
        UniqueConstraint("character_id", name="uq_weapons_character"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    weapon_type: Mapped[str | None] = mapped_column(String(60))
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resonance_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    notes: Mapped[str | None] = mapped_column(Text)


class TeamRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        UniqueConstraint("data_set_id", "name", name="uq_teams_dataset_name"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    notes: Mapped[str | None] = mapped_column(Text)


class TeamMemberRecord(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        CheckConstraint("slot_index >= 0", name="slot_nonnegative"),
        UniqueConstraint("team_id", "slot_index", name="uq_team_members_slot"),
    )

    team_id: Mapped[str] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    character_id: Mapped[str] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str | None] = mapped_column(String(100))


class CharacterSkillRecord(IdMixin, Base):
    __tablename__ = "character_skills"
    __table_args__ = (
        CheckConstraint("current_level >= 0", name="current_nonnegative"),
        CheckConstraint("target_level >= 0", name="target_nonnegative"),
        CheckConstraint("display_order >= 0", name="order_nonnegative"),
        UniqueConstraint("character_id", "skill_key", name="uq_character_skills_key"),
    )

    character_id: Mapped[str] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    skill_type: Mapped[str] = mapped_column(String(40), nullable=False)
    current_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_level: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    is_core: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class StatWeightRecord(IdMixin, Base):
    __tablename__ = "stat_weights"
    __table_args__ = (
        CheckConstraint("weight >= 0", name="weight_nonnegative"),
        UniqueConstraint(
            "scoring_profile_id",
            "stat_name",
            "unit",
            "condition_key",
            name="uq_stat_weights_rule",
        ),
    )

    scoring_profile_id: Mapped[str] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stat_name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    cap_value: Mapped[float | None] = mapped_column(Float)
    condition_key: Mapped[str] = mapped_column(String(120), nullable=False, default="always")
    condition_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CaptureSessionRecord(IdMixin, Base):
    __tablename__ = "capture_sessions"
    __table_args__ = (
        CheckConstraint("monitor_index >= 0", name="monitor_nonnegative"),
        CheckConstraint("sampling_hz BETWEEN 2 AND 8", name="sampling_range"),
        CheckConstraint("stable_wait_ms > 0", name="stable_wait_positive"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    scoring_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="SET NULL"), nullable=True
    )
    backend_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    monitor_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sampling_hz: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    stable_wait_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    app_version: Mapped[str] = mapped_column(String(40), nullable=False)
    roi_config_version: Mapped[str | None] = mapped_column(String(80))
    error_summary: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class EchoRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "echoes"
    __table_args__ = (
        CheckConstraint("cost IS NULL OR cost > 0", name="cost_positive"),
        CheckConstraint("level IS NULL OR level >= 0", name="level_nonnegative"),
        CheckConstraint("slot_index IS NULL OR slot_index BETWEEN 1 AND 5", name="slot_range"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name="score_range"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str | None] = mapped_column(String(160))
    set_name: Mapped[str | None] = mapped_column(String(160))
    cost: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[int | None] = mapped_column(Integer)
    slot_index: Mapped[int | None] = mapped_column(Integer)
    main_stats_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    sub_stats_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    final_status: Mapped[str] = mapped_column(String(40), nullable=False, default="undecided")
    score: Mapped[float | None] = mapped_column(Float)
    score_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    score_profile_version: Mapped[int | None] = mapped_column(Integer)
    score_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("echo_observations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    equipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "uq_echoes_active_character_slot",
    EchoRecord.character_id,
    EchoRecord.slot_index,
    unique=True,
    sqlite_where=and_(
        EchoRecord.character_id.is_not(None),
        EchoRecord.slot_index.is_not(None),
        EchoRecord.archived_at.is_(None),
    ),
)


class EchoSessionRecord(IdMixin, Base):
    __tablename__ = "echo_sessions"
    __table_args__ = (
        UniqueConstraint(
            "capture_session_id", "local_session_id", name="uq_echo_sessions_capture_local"
        ),
    )

    capture_session_id: Mapped[str] = mapped_column(
        ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    echo_id: Mapped[str | None] = mapped_column(
        ForeignKey("echoes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    local_session_id: Mapped[str] = mapped_column(String(80), nullable=False)
    current_state: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    is_discarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    was_consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consumed_into_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("echo_sessions.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class EchoObservationRecord(IdMixin, Base):
    __tablename__ = "echo_observations"
    __table_args__ = (
        CheckConstraint(
            "overall_confidence IS NULL OR overall_confidence BETWEEN 0 AND 1",
            name="confidence_range",
        ),
        UniqueConstraint(
            "echo_session_id", "sequence_no", name="uq_echo_observations_session_sequence"
        ),
    )

    echo_session_id: Mapped[str] = mapped_column(
        ForeignKey("echo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    page_type: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str | None] = mapped_column(String(160))
    level: Mapped[int | None] = mapped_column(Integer)
    projected_level: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[int | None] = mapped_column(Integer)
    main_stats_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    projected_main_stats_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    sub_stats_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    locked_substat_slots: Mapped[int | None] = mapped_column(Integer)
    overall_confidence: Mapped[float | None] = mapped_column(Float)
    confidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    effective_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_timestamp: Mapped[str | None] = mapped_column(String(80))
    source_file: Mapped[str | None] = mapped_column(Text)
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(500))
    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


class EchoEventRecord(IdMixin, Base):
    __tablename__ = "echo_events"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"
        ),
        UniqueConstraint(
            "capture_session_id", "sequence_no", name="uq_echo_events_capture_sequence"
        ),
    )

    capture_session_id: Mapped[str] = mapped_column(
        ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    echo_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("echo_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    related_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("echo_sessions.id", ondelete="SET NULL"), nullable=True
    )
    previous_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("echo_observations.id", ondelete="SET NULL"), nullable=True
    )
    current_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("echo_observations.id", ondelete="SET NULL"), nullable=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    notes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


class ObservationCorrectionRecord(IdMixin, Base):
    __tablename__ = "observation_corrections"

    observation_id: Mapped[str] = mapped_column(
        ForeignKey("echo_observations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_path: Mapped[str] = mapped_column(String(300), nullable=False)
    old_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    new_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(40), nullable=False, default="user")
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class UpgradeDecisionRecord(IdMixin, Base):
    __tablename__ = "upgrade_decisions"
    __table_args__ = (
        CheckConstraint("current_score IS NULL OR current_score BETWEEN 0 AND 100", name="score"),
        CheckConstraint(
            "predicted_min_score IS NULL OR predicted_min_score BETWEEN 0 AND 100",
            name="min_score",
        ),
        CheckConstraint(
            "predicted_max_score IS NULL OR predicted_max_score BETWEEN 0 AND 100",
            name="max_score",
        ),
    )

    echo_session_id: Mapped[str] = mapped_column(
        ForeignKey("echo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("echo_observations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scoring_profile_id: Mapped[str] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    evaluation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observation_payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decision: Mapped[str] = mapped_column(String(80), nullable=False)
    current_score: Mapped[float | None] = mapped_column(Float)
    predicted_min_score: Mapped[float | None] = mapped_column(Float)
    predicted_max_score: Mapped[float | None] = mapped_column(Float)
    probabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    requires_manual_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    decision_inputs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    scoring_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    assumptions_version: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class MaterialRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (
        CheckConstraint(
            "stamina_cost_per_run IS NULL OR stamina_cost_per_run >= 0", name="stamina_nonnegative"
        ),
        CheckConstraint("weekly_limit IS NULL OR weekly_limit >= 0", name="weekly_nonnegative"),
        UniqueConstraint("data_set_id", "code", name="uq_materials_dataset_code"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    stamina_cost_per_run: Mapped[int | None] = mapped_column(Integer)
    average_yield: Mapped[float | None] = mapped_column(Float)
    open_days_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    weekly_limit: Mapped[int | None] = mapped_column(Integer)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    source_version: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)


class InventoryCountRecord(IdMixin, Base):
    __tablename__ = "inventory_counts"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        UniqueConstraint("data_set_id", "material_id", name="uq_inventory_dataset_material"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    material_id: Mapped[str] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class CultivationTargetRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "cultivation_targets"
    __table_args__ = (CheckConstraint("priority >= 0", name="priority_nonnegative"),)

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_key: Mapped[str | None] = mapped_column(String(160))
    current_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    target_value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)


class CultivationTargetMaterialRecord(Base):
    __tablename__ = "cultivation_target_materials"
    __table_args__ = (CheckConstraint("required_quantity >= 0", name="required_nonnegative"),)

    cultivation_target_id: Mapped[str] = mapped_column(
        ForeignKey("cultivation_targets.id", ondelete="CASCADE"), primary_key=True
    )
    material_id: Mapped[str] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), primary_key=True
    )
    required_quantity: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)


class DailyPlanRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "daily_plans"
    __table_args__ = (
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("recommended_runs >= 0", name="runs_nonnegative"),
        CheckConstraint("stamina_cost >= 0", name="stamina_nonnegative"),
        UniqueConstraint("generation_id", "rank", name="uq_daily_plans_generation_rank"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    generation_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_id)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    cultivation_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("cultivation_targets.id", ondelete="SET NULL"), nullable=True
    )
    material_id: Mapped[str | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    recommended_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stamina_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="planned")
    estimated_completion_date: Mapped[date | None] = mapped_column(Date)
    assumptions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class EventRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (CheckConstraint("priority >= 0", name="priority_nonnegative"),)

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    notes: Mapped[str | None] = mapped_column(Text)


class TaskRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (CheckConstraint("priority >= 0", name="priority_nonnegative"),)

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[str | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    task_type: Mapped[str] = mapped_column(String(60), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    recurrence_rule_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    timezone_name: Mapped[str] = mapped_column(String(80), nullable=False, default="Asia/Taipei")
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class GachaStatusRecord(IdMixin, Base):
    __tablename__ = "gacha_status"
    __table_args__ = (
        CheckConstraint("astrite >= 0", name="astrite_nonnegative"),
        CheckConstraint("limited_character_pulls >= 0", name="character_pulls_nonnegative"),
        CheckConstraint("limited_weapon_pulls >= 0", name="weapon_pulls_nonnegative"),
        CheckConstraint("character_pity >= 0", name="character_pity_nonnegative"),
        CheckConstraint("weapon_pity >= 0", name="weapon_pity_nonnegative"),
        UniqueConstraint("data_set_id", name="uq_gacha_status_dataset"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    astrite: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limited_character_pulls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limited_weapon_pulls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    character_pity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weapon_pity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    character_guaranteed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weapon_guaranteed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class GachaTargetRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "gacha_targets"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint("target_copies >= 1", name="copies_positive"),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    target_copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_version: Mapped[str | None] = mapped_column(String(80))
    target_date: Mapped[date | None] = mapped_column(Date, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="planned")
    scenario_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)


class GachaIncomeItemRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "gacha_income_items"
    __table_args__ = (
        CheckConstraint("astrite_amount >= 0", name="astrite_nonnegative"),
        CheckConstraint("pull_amount >= 0", name="pulls_nonnegative"),
        CheckConstraint(
            "pull_type IN ('character', 'weapon', 'unknown')",
            name="pull_type",
        ),
    )

    data_set_id: Mapped[str] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    expected_date: Mapped[date | None] = mapped_column(Date, index=True)
    astrite_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pull_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pull_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown"
    )
    scenario: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    recurrence_rule_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_received: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


REQUIRED_V1_TABLES = frozenset(
    {
        "app_settings",
        "characters",
        "weapons",
        "teams",
        "character_skills",
        "echoes",
        "echo_observations",
        "echo_sessions",
        "echo_events",
        "scoring_profiles",
        "stat_weights",
        "upgrade_decisions",
        "materials",
        "inventory_counts",
        "cultivation_targets",
        "daily_plans",
        "tasks",
        "events",
        "gacha_status",
        "gacha_targets",
        "gacha_income_items",
        "capture_sessions",
        "schema_version",
    }
)


__all__ = ["REQUIRED_V1_TABLES", "Base", "new_id", "utc_now"]
