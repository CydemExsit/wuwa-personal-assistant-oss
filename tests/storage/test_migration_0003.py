from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from wuwa_assistant.storage.engine import create_sqlite_engine
from wuwa_assistant.storage.errors import MigrationError
from wuwa_assistant.storage.migration import _alembic_config, migrate_database


def _upgrade(engine: Engine, revision: str) -> None:
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _insert_material(engine: Engine, identifier: str, data_set_id: str) -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO materials "
                "(id, data_set_id, code, name, category, open_days_json, "
                "source_status, created_at, updated_at) "
                "VALUES (:id, :data_set_id, 'shared-code', :name, 'test', '[]', "
                "'manual', :now, :now)"
            ),
            {
                "id": identifier,
                "data_set_id": data_set_id,
                "name": identifier,
                "now": now,
            },
        )


def _insert_reference_graph(engine: Engine) -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO inventory_counts "
                "(id, data_set_id, material_id, quantity, updated_at) "
                "VALUES ('inventory', 'primary', 'primary-material', 12.25, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO cultivation_targets "
                "(id, data_set_id, target_type, target_value_json, priority, "
                "is_completed, created_at, updated_at) "
                "VALUES ('target', 'primary', 'custom', '1', 1, 0, :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO cultivation_target_materials "
                "(cultivation_target_id, material_id, required_quantity) "
                "VALUES ('target', 'primary-material', 7.5)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO daily_plans "
                "(id, data_set_id, plan_date, generation_id, rank, "
                "cultivation_target_id, material_id, recommended_runs, "
                "stamina_cost, reason, status, assumptions_json, created_at, updated_at) "
                "VALUES ('plan', 'primary', '2026-08-01', 'generation', 1, "
                "'target', 'primary-material', 1, 40, 'reason', 'planned', '{}', :now, :now)"
            ),
            {"now": now},
        )


def test_0003_scopes_material_codes_to_dataset_after_upgrade(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "material-code-migration.sqlite3")
    try:
        _upgrade(engine, "0002_scoring_provenance")
        now = datetime(2026, 8, 1, tzinfo=UTC)
        with engine.begin() as connection:
            for identifier, name, kind in (
                ("primary", "主要資料", "primary"),
                ("demo", "示範資料", "demo"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO data_sets "
                        "(id, name, kind, is_active, created_at) "
                        "VALUES (:id, :name, :kind, 1, :now)"
                    ),
                    {"id": identifier, "name": name, "kind": kind, "now": now},
                )
        _insert_material(engine, "primary-material", "primary")
        _insert_reference_graph(engine)

        _upgrade(engine, "0003_material_codes_per_dataset")

        _insert_material(engine, "demo-material", "demo")
        with pytest.raises(IntegrityError):
            _insert_material(engine, "primary-duplicate", "primary")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT quantity FROM inventory_counts WHERE id='inventory'")
                ).scalar_one()
                == 12.25
            )
            assert (
                connection.execute(
                    text(
                        "SELECT required_quantity FROM cultivation_target_materials "
                        "WHERE cultivation_target_id='target'"
                    )
                ).scalar_one()
                == 7.5
            )
            assert (
                connection.execute(
                    text("SELECT material_id FROM daily_plans WHERE id='plan'")
                ).scalar_one()
                == "primary-material"
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.execute(
                text("SELECT version_num FROM schema_version")
            ).scalar_one() == ("0003_material_codes_per_dataset")
    finally:
        engine.dispose()


def test_0003_rejects_legacy_global_material_without_mutating_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-global-material.sqlite3"
    engine = create_sqlite_engine(database_path)
    try:
        _upgrade(engine, "0002_scoring_provenance")
        now = datetime(2026, 8, 1, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO materials "
                    "(id, data_set_id, code, name, category, open_days_json, "
                    "source_status, created_at, updated_at) "
                    "VALUES ('legacy-global', NULL, 'global', '舊全域素材', 'test', '[]', "
                    "'manual', :now, :now)"
                ),
                {"now": now},
            )

        with pytest.raises(MigrationError, match="legacy materials without a data_set_id"):
            migrate_database(engine, database_path)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM schema_version")
            ).scalar_one() == ("0002_scoring_provenance")
            assert (
                connection.execute(
                    text("SELECT name FROM materials WHERE id='legacy-global'")
                ).scalar_one()
                == "舊全域素材"
            )
    finally:
        engine.dispose()
