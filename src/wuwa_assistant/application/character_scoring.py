"""Resolve a character's explicit profile before any bundled recommendation."""

from __future__ import annotations

from math import isfinite
from typing import Any

from wuwa_assistant.scoring import ScoringContext, StatKey
from wuwa_assistant.scoring.character_guides import guide_for_character

from .scoring_profiles import ScoringProfileService, ScoringProfileSnapshot


def character_profile(
    profiles: ScoringProfileService,
    *,
    data_set_id: str,
    name: str,
    explicit_profile_id: str | None,
    profile_snapshots: tuple[ScoringProfileSnapshot, ...] | None = None,
) -> ScoringProfileSnapshot | None:
    if profile_snapshots is not None:
        if any(
            p.data_set_id != data_set_id and not (p.data_set_id is None and p.is_system_template)
            for p in profile_snapshots
        ):
            raise ValueError("scoring profile does not belong to the requested data set")
        if explicit_profile_id is not None:
            selected = next((p for p in profile_snapshots if p.id == explicit_profile_id), None)
            if selected is not None:
                return selected
            # Keep the existing missing/invisible-profile error behavior.
            return profiles.get_profile(explicit_profile_id, data_set_id=data_set_id)
    if explicit_profile_id is not None:
        return profiles.get_profile(explicit_profile_id, data_set_id=data_set_id)
    guide = guide_for_character(name)
    if guide is None:
        return None
    return next(
        (
            p
            for p in (
                profiles.list_profiles(data_set_id=data_set_id)
                if profile_snapshots is None
                else profile_snapshots
            )
            if p.is_system_template and p.template_kind == guide.template_kind
        ),
        None,
    )


def character_context(goal: dict[str, Any]) -> ScoringContext:
    context = ScoringContext()
    target = goal.get("scoring_energy_target")
    if (
        isinstance(target, (int, float))
        and not isinstance(target, bool)
        and isfinite(target)
        and 100 <= target <= 1000
    ):
        context.conditions["guide_energy_target"] = float(target)
    panel = goal.get("scoring_panel_energy_regen")
    if (
        isinstance(panel, (int, float))
        and not isinstance(panel, bool)
        and isfinite(panel)
        and 100 <= panel <= 1000
    ):
        context.current_totals[StatKey.ENERGY_REGEN] = float(panel)
    return context
