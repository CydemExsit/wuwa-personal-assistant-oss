"""Read-only bridge from local character records to today's farming guidance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmingPlan,
    FarmRole,
    FarmZone,
    recommend_zones,
    resolve_role,
)
from wuwa_assistant.scoring import StatKey
from wuwa_assistant.scoring.character_guides import guide_for_character

from .character_scoring import character_context, character_profile
from .characters import CharacterCultivationService, CharacterDetail
from .echo_sources import OTHER_ECHO_SOURCES
from .equipment_alerts import equipment_fit_alert
from .equipment_overview import CharacterEquipmentOverview
from .scoring_profiles import ScoringProfileService, ScoringProfileSnapshot
from .tacet_catalog import canonical_set_name, load_tacet_catalog


@dataclass(frozen=True, slots=True)
class SkillGap:
    character_id: str
    character_name: str
    skill_key: str
    skill_name: str
    current_level: int
    target_level: int
    is_core: bool


@dataclass(frozen=True, slots=True)
class TodayFarmingSnapshot:
    skills: tuple[SkillGap, ...]
    farming: FarmingPlan
    catalog_note: str
    character_count: int


def _target_sets(detail: CharacterDetail, slot_index: int) -> tuple[str, ...]:
    raw = detail.cultivation_goal.get("echo_set_targets", {})
    if not isinstance(raw, dict):
        return ()
    value = raw.get(str(slot_index), raw.get(slot_index, ()))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(canonical_set_name(name) for name in value if isinstance(name, str) and name)


def _role(detail: CharacterDetail) -> tuple[FarmRole, str]:
    """Prefer explicit character/team functions; team position is not a role."""
    explicit = resolve_role(detail.summary.role or "")
    if explicit is not FarmRole.UNKNOWN:
        return explicit, f"角色設定: {detail.summary.role}"
    memberships = sorted(detail.teams, key=lambda team: (team.team_priority, team.team_name))
    for membership in memberships:
        role = resolve_role(membership.role or "")
        if role is not FarmRole.UNKNOWN:
            return role, f"隊伍 {membership.team_name}: {membership.role}(依隊伍優先順序)"
    guide = guide_for_character(detail.summary.name)
    if guide is not None:
        return resolve_role(guide.role), f"角色攻略: {guide.role}"
    return FarmRole.UNKNOWN, "未設定角色功能; 不從隊伍第幾位推測"


def _farm_character(
    detail: CharacterDetail,
    overview: CharacterEquipmentOverview,
    profiles: ScoringProfileService,
    profile_snapshots: tuple[ScoringProfileSnapshot, ...] | None = None,
) -> FarmCharacter:
    guide = guide_for_character(detail.summary.name)
    role, reason = _role(detail)
    context = character_context(detail.cultivation_goal)
    profile = character_profile(
        profiles,
        data_set_id=detail.summary.data_set_id,
        name=detail.summary.name,
        explicit_profile_id=detail.summary.scoring_profile_id,
        profile_snapshots=profile_snapshots,
    )
    custom_sets = tuple(
        dict.fromkeys(name for slot in range(1, 6) for name in _target_sets(detail, slot))
    )
    sets = custom_sets or (() if guide is None else guide.allowed_sets)
    energy_target = context.conditions.get("guide_energy_target")
    if not isinstance(energy_target, (int, float)):
        energy_target = None if guide is None else guide.energy_target
    return FarmCharacter(
        identifier=detail.summary.id,
        name=detail.summary.name,
        role=role,
        role_reason=reason,
        sets=tuple(canonical_set_name(name) for name in sets),
        echoes=tuple(
            FarmEcho(
                slot_index=echo.slot_index or 0,
                cost=echo.cost,
                level=echo.level,
                score=echo.score,
                set_name=None if echo.set_name is None else canonical_set_name(echo.set_name),
                set_valid=echo.set_is_valid,
                main_valid=echo.main_stat_is_valid,
                equipped=echo.echo_id is not None,
                target_sets=_target_sets(detail, echo.slot_index or 0),
                fit_reason=equipment_fit_alert(echo),
            )
            for echo in overview.slots
        ),
        set_combinations=()
        if guide is None or custom_sets
        else tuple(
            tuple((canonical_set_name(name), count) for name, count in combination)
            for combination in guide.set_combinations
        ),
        passing_score=70.0 if profile is None else profile.thresholds.passing,
        energy_current=context.current_totals.get(StatKey.ENERGY_REGEN),
        energy_target=energy_target,
        priority=detail.summary.priority,
        check_set_combination=not custom_sets,
        best_team_priority=min(
            (membership.team_priority for membership in detail.teams), default=None
        ),
    )


def build_today_farming(
    characters: CharacterCultivationService,
    profiles: ScoringProfileService,
    *,
    data_set_id: str,
    tacet_cache_directory: Path | None = None,
    as_of: date | None = None,
    stocked_field_ids: frozenset[str] = frozenset(),
    requested_slots: Mapping[tuple[str, int], int] | None = None,
) -> TodayFarmingSnapshot:
    """Use the same automatic per-character score/fit as the Echo overview."""
    requested_characters = {character_id for character_id, _ in requested_slots or {}}
    details = tuple(
        detail
        for detail in characters.list_character_details(
            data_set_id=data_set_id, candidates_only=not requested_characters
        )
        if detail.summary.is_candidate or detail.summary.id in requested_characters
    )
    skills = tuple(
        SkillGap(
            detail.summary.id,
            detail.summary.name,
            skill.skill_key,
            skill.name,
            skill.current_level,
            skill.target_level,
            skill.is_core,
        )
        for detail in sorted(details, key=lambda row: (row.summary.priority, row.summary.name))
        if detail.summary.is_candidate
        for skill in sorted(
            detail.skills,
            key=lambda row: (
                0
                if detail.cultivation_goal.get("skill_order_custom") is True
                else -row.target_level,
                row.display_order,
            ),
        )
        if skill.current_level < skill.target_level
    )
    overview = characters.equipment_overview(data_set_id=data_set_id, character_details=details)
    profile_snapshots = profiles.list_profiles(data_set_id=data_set_id)
    by_id = {row.character_id: row for row in overview.characters}
    catalog = load_tacet_catalog(tacet_cache_directory)
    planning_date = as_of or date.today()
    zones = tuple(
        FarmZone(
            field.field_id,
            field.name,
            field.region,
            tuple(canonical_set_name(name) for name in field.set_names),
            field.source_url,
        )
        for field in catalog.fields
        if field.verified
        and (
            field.available_from is None
            or date.fromisoformat(field.available_from) <= planning_date
        )
    )
    farm_characters = tuple(
        _farm_character(detail, by_id[detail.summary.id], profiles, profile_snapshots)
        for detail in details
        if detail.summary.id in by_id
    )
    if requested_slots is not None:
        zones = (*zones, *OTHER_ECHO_SOURCES)
    plan = recommend_zones(
        farm_characters,
        zones,
        stocked_field_ids=stocked_field_ids,
        requested_slots=requested_slots,
    )
    role_notes = tuple(f"{row.name}: {row.role_reason}" for row in farm_characters)
    return TodayFarmingSnapshot(
        skills,
        FarmingPlan(plan.recommendations, (*plan.notices, *role_notes), plan.policy_version),
        catalog.coverage_note
        if requested_slots is None
        else ("刷取表只列有來源對應的需求。其餘請展開資料待確認項目。"),
        len(details),
    )
