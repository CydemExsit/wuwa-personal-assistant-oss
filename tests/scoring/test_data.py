from __future__ import annotations

import json

from wuwa_assistant.scoring import (
    ProvenanceStatus,
    StatKey,
    load_decision_policy,
    load_scoring_catalog,
    load_template_seed_definitions,
)


def test_catalog_contains_four_versioned_editable_templates() -> None:
    catalog = load_scoring_catalog()

    assert catalog.data_version == "scoring-templates-2026.09-v3"
    assert {profile.template_kind for profile in catalog.profiles} == {
        "generic_dps",
        "healing_hp",
        "defense",
        "energy_requirement",
    }
    assert sum(profile.is_default for profile in catalog.profiles) == 1
    assert all(
        profile.roll_data_version == catalog.rolls.data_version for profile in catalog.profiles
    )
    assert set(catalog.rolls.roll_values) == set(StatKey)

    generic = catalog.profile("generic_dps_v1")
    assert generic.version == 3
    assert (
        generic.thresholds.usable,
        generic.thresholds.passing,
        generic.thresholds.perfect,
        generic.thresholds.exceptional,
    ) == (50.0, 65.0, 85.0, 93.0)
    for profile in catalog.profiles:
        if profile.template_kind != "generic_dps":
            assert profile.version == 2
            assert (
                profile.thresholds.usable,
                profile.thresholds.passing,
                profile.thresholds.perfect,
                profile.thresholds.exceptional,
            ) == (55.0, 70.0, 88.0, 95.0)


def test_legacy_roll_probability_is_explicit_non_official_uniform_fallback() -> None:
    from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION, load_roll_data

    rolls = load_roll_data(LEGACY_ROLL_VERSION)

    assert rolls.provenance.status is ProvenanceStatus.COMMUNITY_OBSERVED_UNVERIFIED
    assert not rolls.provenance.is_official
    assert rolls.probability_assumption.status is ProvenanceStatus.UNIFORM_FALLBACK
    assert not rolls.probability_assumption.is_official
    assert "No official" in rolls.probability_assumption.notes


def test_decision_policy_is_versioned_non_official_and_calibrated() -> None:
    policy = load_decision_policy()

    assert policy.schema_version == 6
    assert policy.data_version == "upgrade-decision-policy-2026.10-v6"
    assert policy.provenance.status is ProvenanceStatus.PROJECT_DEFAULT
    assert not policy.provenance.is_official
    assert policy.decision_levels == {5, 10, 15, 20, 25}
    assert policy.base_continue_chance == {5: 0.08, 10: 0.10, 15: 0.16, 20: 0.23}
    assert policy.adjustments.embryo_supply_abundant == 0.12
    assert policy.adjustments.embryo_supply_scarce == -0.04
    assert policy.adjustments.breadth_first_gap == -0.08
    assert policy.adjustments.character_already_qualified == 0.12
    assert policy.adjustments.many_candidates_at_least == 8
    assert policy.adjustments.many_candidates == 0.05
    assert policy.adjustments.few_candidates_at_most == 1
    assert policy.adjustments.few_candidates == -0.04
    assert policy.adjustments.current_score_at_least_usable == -0.04
    assert (policy.minimum_continue_chance, policy.maximum_continue_chance) == (0.02, 0.75)
    assert policy.weak_baseline_quality_level == policy.exploration_level == 15


def test_seed_definitions_are_json_safe_fresh_and_storage_compatible() -> None:
    first = load_template_seed_definitions()
    encoded = json.dumps(first, ensure_ascii=False)
    second = load_template_seed_definitions()

    assert "通用輸出型" in encoded
    assert len(first) == 4
    assert all(
        {
            "name",
            "template_kind",
            "version",
            "thresholds",
            "threshold_rules",
            "crit_balance_rule",
            "roll_data_version",
            "weights",
            "provenance",
        }
        <= definition.keys()
        for definition in first
    )
    generic = next(item for item in first if item["template_kind"] == "generic_dps")
    assert any(row["condition_key"] == "uses_basic_attack" for row in generic["weights"])
    assert all("stat_name" in row and "unit" in row for row in generic["weights"])

    first[0]["name"] = "mutated"
    assert second[0]["name"] != "mutated"
