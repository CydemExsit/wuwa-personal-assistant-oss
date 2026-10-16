from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from wuwa_assistant.application.character_scoring import character_profile
from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.scoring_profiles import (
    ScoringProfileService,
    ScoringProfileSnapshot,
)
from wuwa_assistant.scoring.character_guides import (
    AUTO_PROFILE_ID,
    load_character_seed_definitions,
)
from wuwa_assistant.storage import AppPaths, create_database

# The released eight-character v2 priorities, independent of the incoming data.
_PREVIOUS_WEIGHTS = {
    "1209": {
        "energy_regen": 1,
        "resonance_liberation_damage": 0.8,
        "defense_pct": 0.6,
        "crit_damage": 0.4,
        "flat_defense": 0.25,
    },
    "1108": {
        "crit_rate": 1,
        "crit_damage": 1,
        "attack_pct": 0.8,
        "resonance_liberation_damage": 0.6,
        "energy_regen": 0.6,
    },
    "1109": {"crit_rate": 1, "crit_damage": 1, "attack_pct": 0.8, "basic_attack_damage": 0.6},
    "1110": {
        "energy_regen": 1,
        "hp_pct": 0.8,
        "crit_damage": 0.6,
        "crit_rate": 0.4,
        "flat_hp": 0.25,
    },
    "1210": {
        "crit_rate": 1,
        "crit_damage": 1,
        "attack_pct": 0.8,
        "resonance_liberation_damage": 0.6,
        "energy_regen": 0.4,
        "flat_attack": 0.25,
    },
    "1211": {
        "crit_rate": 1,
        "crit_damage": 1,
        "energy_regen": 0.8,
        "attack_pct": 0.6,
        "resonance_liberation_damage": 0.4,
        "flat_attack": 0.25,
    },
    "1413": {
        "crit_rate": 1,
        "crit_damage": 1,
        "attack_pct": 0.8,
        "energy_regen": 0.6,
        "flat_attack": 0.4,
        "heavy_attack_damage": 0.25,
        "basic_attack_damage": 0.15,
        "resonance_liberation_damage": 0.15,
    },
    "1610": {
        "crit_rate": 1,
        "crit_damage": 1,
        "attack_pct": 0.8,
        "heavy_attack_damage": 0.6,
        "energy_regen": 0.4,
        "flat_attack": 0.25,
    },
}


def _previous_definitions(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous = []
    for definition in deepcopy(current):
        role_id = definition["template_kind"].removeprefix("character_guide_")
        if role_id not in _PREVIOUS_WEIGHTS:
            continue
        definition["version"] = 2
        definition["description"] = "Previously released character reference"
        for row in definition["weights"]:
            row["weight"] = _PREVIOUS_WEIGHTS[role_id].get(row["stat_name"], 0)
        previous.append(definition)
    assert len(previous) == 8
    return previous


def _weights(snapshot: ScoringProfileSnapshot) -> dict[str, float]:
    return {row.stat_name: row.weight for row in snapshot.weights}


def test_nineteen_guide_upgrade_preserves_user_copies_and_explicit_selections(
    tmp_path: Path,
) -> None:
    incoming = load_character_seed_definitions()
    with create_database(AppPaths.from_root(tmp_path / "guide-upgrade")) as database:
        data_set_id = EchoHistoryService(database.session_factory).ensure_active_primary_dataset()
        profiles = ScoringProfileService(database.session_factory)
        characters = CharacterCultivationService(database.session_factory)
        previous = {
            row.template_kind: row
            for row in profiles.ensure_system_templates(_previous_definitions(incoming))
        }
        old_aemeath = previous["character_guide_1210"]
        unchanged_copy = profiles.clone_template(
            old_aemeath.id, data_set_id=data_set_id, name="保留舊版愛彌斯需求"
        )
        edited_copy = profiles.clone_template(
            old_aemeath.id,
            data_set_id=data_set_id,
            name="我的愛彌斯效率取捨",
            make_default=True,
        )
        custom_weights = edited_copy.to_json_dict()["weights"]
        for weight in custom_weights:
            if weight["stat_name"] == "energy_regen":
                weight["weight"] = 0.37
        edited_copy = profiles.replace_weights(
            edited_copy.id, data_set_id=data_set_id, weights=custom_weights
        )
        saved_copies = {
            row.id: profiles.get_profile(row.id, data_set_id=data_set_id).to_json_dict()
            for row in (unchanged_copy, edited_copy)
        }
        explicit_ids = {
            "愛彌斯": edited_copy.id,
            "緋雪": previous["character_guide_1108"].id,
        }
        owners = {
            name: characters.create_character(
                data_set_id=data_set_id,
                command=CreateCharacter(name=name, scoring_profile_id=explicit_ids.get(name)),
            ).summary.id
            for name in (
                "洛瑟菈",
                "秧秧·玄翎",
                "穗穗",
                "清宵",
                "緋雪",
                "莫寧",
                "愛彌斯",
                "達妮婭",
                "千咲",
            )
        }
        assert (
            character_profile(
                profiles, data_set_id=data_set_id, name="千咲", explicit_profile_id=None
            )
            is None
        )
        assert _weights(old_aemeath)["energy_regen"] == 0.4

        upgraded = {row.template_kind: row for row in profiles.ensure_system_templates(incoming)}
        assert set(upgraded) == {row["template_kind"] for row in incoming}
        assert len(upgraded) == 20
        assert all(row.version == 4 for row in upgraded.values())
        assert all(upgraded[kind].id == old.id for kind, old in previous.items())
        assert upgraded["character_guide_1508"].id not in {p.id for p in previous.values()}
        mornye = _weights(upgraded["character_guide_1209"])
        assert {
            stat: mornye[stat]
            for stat in (
                "energy_regen",
                "defense_pct",
                "resonance_liberation_damage",
                "crit_damage",
                "crit_rate",
                "flat_defense",
            )
        } == {
            "energy_regen": 1,
            "defense_pct": 0.8,
            "resonance_liberation_damage": 0.6,
            "crit_damage": 0.6,
            "crit_rate": 0.4,
            "flat_defense": 0.25,
        }
        assert _weights(upgraded["character_guide_1210"])["energy_regen"] == 0.8
        assert _weights(upgraded["character_guide_1210"])["flat_attack"] == 0.25

        # Automatic resolution sees the upgraded system record, while a user's
        # explicitly selected copy remains authoritative even for a known role.
        for name, owner_id in owners.items():
            automatic = character_profile(
                profiles, data_set_id=data_set_id, name=name, explicit_profile_id=None
            )
            assert automatic is not None and automatic.version == 4
            detail = characters.get_character(owner_id, data_set_id=data_set_id)
            assert detail.summary.scoring_profile_id == explicit_ids.get(name)
            resolved = character_profile(
                profiles,
                data_set_id=data_set_id,
                name=name,
                explicit_profile_id=detail.summary.scoring_profile_id,
            )
            assert resolved is not None
            assert resolved.id == explicit_ids.get(name, automatic.id)
        overview = characters.equipment_overview(
            data_set_id=data_set_id, profile_id=AUTO_PROFILE_ID
        )
        aemeath = next(row for row in overview.characters if row.character_name == "愛彌斯")
        chisaki = next(row for row in overview.characters if row.character_name == "千咲")
        assert aemeath.profile_name == edited_copy.name and not aemeath.profile_is_system
        assert chisaki.template_kind == "character_guide_1508" and chisaki.profile_is_system

        before_repeat = {
            row.id: row.to_json_dict() for row in profiles.list_profiles(data_set_id=data_set_id)
        }
        profiles.ensure_system_templates(incoming)
        assert {
            row.id: row.to_json_dict() for row in profiles.list_profiles(data_set_id=data_set_id)
        } == before_repeat
        for profile_id, saved in saved_copies.items():
            assert profiles.get_profile(profile_id, data_set_id=data_set_id).to_json_dict() == saved
        default = profiles.get_default(data_set_id=data_set_id)
        assert default is not None and default.id == edited_copy.id
        assert (
            _weights(profiles.get_profile(unchanged_copy.id, data_set_id=data_set_id))[
                "energy_regen"
            ]
            == 0.4
        )
        assert (
            _weights(profiles.get_profile(edited_copy.id, data_set_id=data_set_id))["energy_regen"]
            == 0.37
        )
