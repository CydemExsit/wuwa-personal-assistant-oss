from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

import wuwa_assistant.application.scoring_profiles as profile_module
from wuwa_assistant.application.scoring_profiles import (
    ScoreThresholds,
    ScoringProfileDefinition,
    ScoringProfileService,
    StatWeightDefinition,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    DataSetRecord,
    ScoringProfileRecord,
    StatWeightRecord,
)


@pytest.fixture
def storage(
    tmp_path: Path,
) -> Iterator[tuple[Database, ScoringProfileService, str, str]]:
    database = create_database(AppPaths.from_root(tmp_path / "profiles"))
    with database.session_factory.begin() as session:
        primary = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        demo = DataSetRecord(name="示範資料", kind="demo", is_active=False)
        session.add_all([primary, demo])
        session.flush()
        primary_id = primary.id
        demo_id = demo.id
    try:
        yield (
            database,
            ScoringProfileService(database.session_factory),
            primary_id,
            demo_id,
        )
    finally:
        database.dispose()


def _system_templates(*, version: int = 1, crit_weight: float = 1.0) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for index, (kind, name) in enumerate(
        (
            ("generic_dps", "通用輸出型"),
            ("healing_hp", "治療/生命型"),
            ("defense", "防禦型"),
            ("energy", "共鳴效率需求型"),
        )
    ):
        definitions.append(
            {
                "name": name,
                "template_kind": kind,
                "description": f"template {kind}",
                "version": version,
                "is_default": index == 0,
                "thresholds": {
                    "usable": 55,
                    "passing": 70,
                    "perfect": 88,
                    "exceptional": 95,
                },
                "threshold_rules": [{"kind": "minimum", "score": 55}],
                "crit_balance_rule": {"target_ratio": 2.0, "enabled": True},
                "roll_data_version": "uniform-fallback-v1",
                "provenance": {"status": "fallback"},
                "conditional_weights": [{"stat_name": "crit_rate"}],
                "weights": [
                    {
                        "stat_name": "crit_rate",
                        "unit": "%",
                        "weight": crit_weight,
                        "condition_key": None,
                        "condition": None,
                        "display_order": 0,
                    },
                    {
                        "stat_name": "energy_regen",
                        "unit": "%",
                        "weight": 0.4,
                        "condition_key": None,
                        "condition": None,
                        "display_order": 1,
                    },
                    {
                        "stat_name": "energy_regen",
                        "unit": "%",
                        "weight": 0.6,
                        "cap_value": 140,
                        "condition_key": "below_energy_target",
                        "condition": {"field": "energy_regen", "lt": 140},
                        "display_order": 2,
                    },
                ],
            }
        )
    return definitions


def _user_definition(name: str = "我的輸出設定") -> ScoringProfileDefinition:
    return ScoringProfileDefinition(
        name=name,
        template_kind="custom_dps",
        description="可自行編輯",
        thresholds=ScoreThresholds(54, 69, 87, 94),
        threshold_rules=({"kind": "energy_floor", "value": 120},),
        crit_balance_rule={"target_ratio": 2.0, "tolerance": 0.2},
        roll_data_version="rolls-v1",
    )


def _weights(value: float = 1.0) -> tuple[StatWeightDefinition, ...]:
    return (
        StatWeightDefinition("crit_rate", value, "%", display_order=0),
        StatWeightDefinition("energy_regen", 0.4, "%", display_order=1),
        StatWeightDefinition(
            "energy_regen",
            0.5,
            "%",
            cap_value=140,
            condition_key="below_target",
            condition={"lt": 140},
            display_order=2,
        ),
    )


def test_system_templates_are_idempotent_versioned_and_json_safe(
    storage: tuple[Database, ScoringProfileService, str, str],
) -> None:
    database, service, primary_id, _demo_id = storage

    first = service.ensure_system_templates(_system_templates())
    repeated = service.ensure_system_templates(_system_templates(crit_weight=9.0))

    assert len(first) == 4
    assert [item.id for item in repeated] == [item.id for item in first]
    assert repeated[0].weights[0].weight == 1.0
    assert repeated[0].weights[0].condition_key == "always"
    assert service.get_default(data_set_id=primary_id).id == first[0].id  # type: ignore[union-attr]
    json.dumps(first[0].to_json_dict(), ensure_ascii=False, allow_nan=False)
    user_profile = service.clone_template(
        first[0].id,
        data_set_id=primary_id,
        name="Preserved user profile",
    )
    user_before = user_profile.to_json_dict()

    upgraded = service.ensure_system_templates(_system_templates(version=2, crit_weight=1.5))
    assert upgraded[0].id == first[0].id
    assert upgraded[0].version == 2
    assert upgraded[0].weights[0].weight == 1.5
    assert (
        service.get_profile(
            user_profile.id,
            data_set_id=primary_id,
        ).to_json_dict()
        == user_before
    )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScoringProfileRecord)) == 5
        assert session.scalar(select(func.count()).select_from(StatWeightRecord)) == 15


def test_user_profile_persists_across_database_restart(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "restart")
    database = create_database(paths)
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        data_set_id = data_set.id
    service = ScoringProfileService(database.session_factory)
    created = service.create_user_profile(
        data_set_id=data_set_id,
        definition=_user_definition(),
        weights=_weights(),
        make_default=True,
    )
    database.dispose()

    reopened = create_database(paths)
    try:
        restored = ScoringProfileService(reopened.session_factory).get_profile(
            created.id,
            data_set_id=data_set_id,
        )
        assert restored.to_json_dict() == created.to_json_dict()
        assert restored.threshold_rules == ({"kind": "energy_floor", "value": 120},)
        assert restored.weights[2].condition == {"lt": 140}
    finally:
        reopened.dispose()


def test_crud_clone_defaults_and_dataset_separation(
    storage: tuple[Database, ScoringProfileService, str, str],
) -> None:
    _database, service, primary_id, demo_id = storage
    templates = service.ensure_system_templates(_system_templates())
    system = templates[0]
    primary = service.create_user_profile(
        data_set_id=primary_id,
        definition=_user_definition("同名設定"),
        weights=_weights(),
        make_default=True,
    )
    demo = service.create_user_profile(
        data_set_id=demo_id,
        definition=_user_definition("同名設定"),
        weights=_weights(0.7),
        make_default=True,
    )

    assert {item.id for item in service.list_profiles(data_set_id=primary_id)} == {
        *[item.id for item in templates],
        primary.id,
    }
    assert demo.id not in {item.id for item in service.list_profiles(data_set_id=primary_id)}
    with pytest.raises(LookupError):
        service.get_profile(demo.id, data_set_id=primary_id)

    clone = service.clone_template(
        system.id,
        data_set_id=primary_id,
        name="通用輸出型 (自訂)",
        make_default=True,
    )
    assert not clone.is_system_template
    assert clone.data_set_id == primary_id
    assert clone.weights[0].stat_name == "crit_rate"
    assert service.get_default(data_set_id=primary_id).id == clone.id  # type: ignore[union-attr]
    assert service.get_default(data_set_id=demo_id).id == demo.id  # type: ignore[union-attr]
    assert not service.get_profile(primary.id, data_set_id=primary_id).is_default

    updated = service.update_user_profile(
        clone.id,
        data_set_id=primary_id,
        definition=_user_definition("角色專用"),
        weights=[{"stat_name": "crit_damage", "unit": "%", "weight": 1.2}],
    )
    assert updated.name == "角色專用"
    assert updated.version == 2
    assert [weight.stat_name for weight in updated.weights] == ["crit_damage"]
    assert service.delete_user_profile(updated.id, data_set_id=primary_id)
    with pytest.raises(LookupError):
        service.get_profile(updated.id, data_set_id=primary_id)


def test_replace_weights_rolls_back_if_transaction_fails(
    storage: tuple[Database, ScoringProfileService, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, primary_id, _demo_id = storage
    created = service.create_user_profile(
        data_set_id=primary_id,
        definition=_user_definition(),
        weights=_weights(),
    )
    original_replace = profile_module._replace_weight_records

    def fail_after_delete(
        session: Any,
        profile_id: str,
        definitions: Any,
    ) -> None:
        for record in profile_module._weight_records(session, profile_id):
            session.delete(record)
        session.flush()
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(profile_module, "_replace_weight_records", fail_after_delete)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        service.replace_weights(
            created.id,
            data_set_id=primary_id,
            weights=[StatWeightDefinition("attack_pct", 0.8, "%")],
        )
    monkeypatch.setattr(profile_module, "_replace_weight_records", original_replace)

    restored = service.get_profile(created.id, data_set_id=primary_id)
    assert restored.version == 1
    assert [(item.stat_name, item.weight) for item in restored.weights] == [
        ("crit_rate", 1.0),
        ("energy_regen", 0.4),
        ("energy_regen", 0.5),
    ]


def test_system_templates_are_protected_but_cloneable(
    storage: tuple[Database, ScoringProfileService, str, str],
) -> None:
    _database, service, primary_id, _demo_id = storage
    system = service.ensure_system_templates(_system_templates())[2]

    with pytest.raises(PermissionError, match="read-only"):
        service.update_user_profile(
            system.id,
            data_set_id=primary_id,
            definition=_user_definition(),
        )
    with pytest.raises(PermissionError, match="read-only"):
        service.replace_weights(
            system.id,
            data_set_id=primary_id,
            weights=_weights(),
        )
    selected = service.set_default(system.id, data_set_id=primary_id)
    assert selected.id == system.id
    assert selected.is_default
    with pytest.raises(PermissionError, match="read-only"):
        service.delete_user_profile(system.id, data_set_id=primary_id)

    clone = service.clone_template(system.id, data_set_id=primary_id, name="可編輯副本")
    assert [
        (
            item.stat_name,
            item.unit,
            item.weight,
            item.cap_value,
            item.condition_key,
            item.condition,
            item.display_order,
        )
        for item in clone.weights
    ] == [
        (
            item.stat_name,
            item.unit,
            item.weight,
            item.cap_value,
            item.condition_key,
            item.condition,
            item.display_order,
        )
        for item in system.weights
    ]


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        (
            {
                "name": "錯誤門檻",
                "thresholds": {
                    "usable": 55,
                    "passing": 90,
                    "perfect": 80,
                    "exceptional": 95,
                },
            },
            "must be ordered",
        ),
        (
            {
                "name": "非 JSON",
                "crit_balance_rule": {"ratio": float("nan")},
            },
            "finite JSON",
        ),
    ],
)
def test_invalid_profile_data_is_rejected_without_a_partial_write(
    storage: tuple[Database, ScoringProfileService, str, str],
    definition: dict[str, Any],
    message: str,
) -> None:
    database, service, primary_id, _demo_id = storage
    with pytest.raises(ValueError, match=message):
        service.create_user_profile(
            data_set_id=primary_id,
            definition=definition,
            weights=_weights(),
        )
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScoringProfileRecord)) == 0


@pytest.mark.parametrize(
    ("weights", "message"),
    [
        (
            [{"stat_name": "unknown_stat", "unit": "%", "weight": 1.0}],
            "unknown canonical stat name",
        ),
        (
            [{"stat_name": "crit_rate", "unit": "", "weight": 1.0}],
            "unit .* is invalid",
        ),
        (
            [
                {
                    "stat_name": "crit_rate",
                    "unit": "%",
                    "weight": 1.0,
                    "cap_value": 0,
                }
            ],
            "cap value must be positive",
        ),
        (
            [{"stat_name": "crit_rate", "unit": "%", "weight": 0.0}],
            "at least one positive base weight",
        ),
        (
            [
                {"stat_name": "attack_pct", "unit": "%", "weight": 1.0},
                {
                    "stat_name": "crit_rate",
                    "unit": "%",
                    "weight": 1.2,
                    "condition_key": "burst_mode",
                },
            ],
            "conditional weight requires a positive base weight",
        ),
    ],
)
def test_invalid_weight_domain_is_rejected_before_persistence(
    storage: tuple[Database, ScoringProfileService, str, str],
    weights: list[dict[str, Any]],
    message: str,
) -> None:
    database, service, primary_id, _demo_id = storage

    with pytest.raises(ValueError, match=message):
        service.create_user_profile(
            data_set_id=primary_id,
            definition=_user_definition("Invalid profile"),
            weights=weights,
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ScoringProfileRecord)) == 0


def test_duplicate_replacement_rules_leave_previous_weights_untouched(
    storage: tuple[Database, ScoringProfileService, str, str],
) -> None:
    _database, service, primary_id, _demo_id = storage
    created = service.create_user_profile(
        data_set_id=primary_id,
        definition=_user_definition(),
        weights=_weights(),
    )

    duplicate = {"stat_name": "crit_rate", "unit": "%", "weight": 1.0}
    with pytest.raises(ValueError, match="must be unique"):
        service.replace_weights(
            created.id,
            data_set_id=primary_id,
            weights=[duplicate, duplicate],
        )

    restored = service.get_profile(created.id, data_set_id=primary_id)
    assert restored.version == 1
    assert len(restored.weights) == 3


def test_scoring_package_seed_contract_round_trips(
    storage: tuple[Database, ScoringProfileService, str, str],
) -> None:
    from wuwa_assistant.scoring.data import load_template_seed_definitions

    _database, service, _primary_id, _demo_id = storage
    profiles = service.ensure_system_templates(load_template_seed_definitions())

    assert [profile.template_kind for profile in profiles] == [
        "generic_dps",
        "healing_hp",
        "defense",
        "energy_requirement",
    ]
    assert all(profile.weights for profile in profiles)
    assert all(profile.version >= 1 for profile in profiles)
    for profile in profiles:
        json.dumps(profile.to_json_dict(), ensure_ascii=False, allow_nan=False)
