from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from wuwa_assistant.storage.engine import create_sqlite_engine
from wuwa_assistant.storage.migration import _alembic_config

_REVISION_0003 = "0003_material_codes_per_dataset"
_REVISION_0004 = "0004_tasks_and_gacha_v1"
_NOW = datetime(2026, 8, 8, 6, 0, tzinfo=UTC)


def _upgrade(engine: Engine, revision: str) -> None:
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _downgrade(engine: Engine, revision: str) -> None:
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def _seed_dataset(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO data_sets (id, name, kind, is_active, created_at) "
                "VALUES ('primary', 'Primary', 'primary', 1, :now)"
            ),
            {"now": _NOW},
        )


def _seed_event_and_task(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO events "
                "(id, data_set_id, title, event_type, starts_at, ends_at, priority, "
                "status, is_hidden, source_status, notes, created_at, updated_at) "
                "VALUES ('event', 'primary', 'Event', 'limited', :starts_at, :ends_at, "
                "1, 'active', 0, 'manual', 'event notes', :now, :now)"
            ),
            {
                "starts_at": datetime(2026, 8, 8, 8, tzinfo=UTC),
                "ends_at": datetime(2026, 8, 20, 8, tzinfo=UTC),
                "now": _NOW,
            },
        )
        connection.execute(
            text(
                "INSERT INTO tasks "
                "(id, data_set_id, event_id, title, task_type, due_at, snoozed_until, "
                "completed_at, priority, recurrence_rule_json, timezone_name, is_hidden, "
                "notes, created_at, updated_at) "
                "VALUES ('task', 'primary', 'event', 'Task', 'event', :due_at, "
                ":snoozed_until, NULL, 2, NULL, 'Asia/Taipei', 0, 'task notes', :now, :now)"
            ),
            {
                "due_at": datetime(2026, 8, 18, 8, tzinfo=UTC),
                "snoozed_until": datetime(2026, 8, 9, 8, tzinfo=UTC),
                "now": _NOW,
            },
        )


def _insert_income(
    engine: Engine,
    *,
    identifier: str,
    include_pull_type: bool,
    pull_type: str = "unknown",
) -> None:
    columns = ""
    values = ""
    parameters: dict[str, object] = {
        "identifier": identifier,
        "expected_date": date(2026, 8, 15),
        "now": _NOW,
    }
    if include_pull_type:
        columns = ", pull_type"
        values = ", :pull_type"
        parameters["pull_type"] = pull_type
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO gacha_income_items "
                "(id, data_set_id, label, expected_date, astrite_amount, pull_amount, "
                "scenario, recurrence_rule_json, is_received, notes, created_at, updated_at"
                f"{columns}) VALUES (:identifier, 'primary', 'Income', :expected_date, "
                f"160, 2, 'general', NULL, 0, 'income notes', :now, :now{values})"
            ),
            parameters,
        )


def _foreign_key_action(engine: Engine) -> str:
    foreign_keys = {
        tuple(item["constrained_columns"]): item
        for item in inspect(engine).get_foreign_keys("tasks")
    }
    return str(foreign_keys[("event_id",)]["options"]["ondelete"])


def test_0004_upgrades_legacy_rows_and_retains_tasks_when_event_is_deleted(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "tasks-gacha-upgrade.sqlite3")
    try:
        _upgrade(engine, _REVISION_0003)
        _seed_dataset(engine)
        _seed_event_and_task(engine)
        _insert_income(engine, identifier="legacy-income", include_pull_type=False)

        _upgrade(engine, _REVISION_0004)

        schema = inspect(engine)
        assert _foreign_key_action(engine) == "SET NULL"
        assert {column["name"] for column in schema.get_columns("gacha_income_items")} >= {
            "pull_type"
        }
        assert {item["name"] for item in schema.get_indexes("events")} >= {"ix_events_starts_at"}
        assert {item["name"] for item in schema.get_indexes("tasks")} >= {
            "ix_tasks_snoozed_until",
            "ix_tasks_completed_at",
        }
        assert {item["name"] for item in schema.get_indexes("gacha_targets")} >= {
            "ix_gacha_targets_target_date"
        }
        assert {item["name"] for item in schema.get_indexes("gacha_income_items")} >= {
            "ix_gacha_income_items_expected_date"
        }

        with engine.connect() as connection:
            income = connection.execute(
                text(
                    "SELECT astrite_amount, pull_amount, pull_type, notes "
                    "FROM gacha_income_items WHERE id='legacy-income'"
                )
            ).one()
            assert income == (160, 2, "unknown", "income notes")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        _insert_income(
            engine,
            identifier="character-income",
            include_pull_type=True,
            pull_type="character",
        )
        _insert_income(
            engine,
            identifier="weapon-income",
            include_pull_type=True,
            pull_type="weapon",
        )
        with pytest.raises(IntegrityError):
            _insert_income(
                engine,
                identifier="invalid-income",
                include_pull_type=True,
                pull_type="standard",
            )

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE id='event'"))
        with engine.connect() as connection:
            task = connection.execute(
                text("SELECT event_id, title, notes FROM tasks WHERE id='task'")
            ).one()
            assert task == (None, "Task", "task notes")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.execute(
                text("SELECT version_num FROM schema_version")
            ).scalar_one() == (_REVISION_0004)
    finally:
        engine.dispose()


def test_0004_downgrade_preserves_rows_and_restores_legacy_foreign_key(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "tasks-gacha-downgrade.sqlite3")
    try:
        _upgrade(engine, _REVISION_0004)
        _seed_dataset(engine)
        _seed_event_and_task(engine)
        _insert_income(
            engine,
            identifier="weapon-income",
            include_pull_type=True,
            pull_type="weapon",
        )

        _downgrade(engine, _REVISION_0003)

        schema = inspect(engine)
        assert _foreign_key_action(engine) == "CASCADE"
        assert "pull_type" not in {
            column["name"] for column in schema.get_columns("gacha_income_items")
        }
        assert "ix_events_starts_at" not in {item["name"] for item in schema.get_indexes("events")}
        assert "ix_tasks_snoozed_until" not in {
            item["name"] for item in schema.get_indexes("tasks")
        }
        assert "ix_tasks_completed_at" not in {item["name"] for item in schema.get_indexes("tasks")}
        assert "ix_gacha_targets_target_date" not in {
            item["name"] for item in schema.get_indexes("gacha_targets")
        }
        assert "ix_gacha_income_items_expected_date" not in {
            item["name"] for item in schema.get_indexes("gacha_income_items")
        }

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT event_id, title, notes FROM tasks WHERE id='task'")
            ).one() == ("event", "Task", "task notes")
            assert connection.execute(
                text(
                    "SELECT astrite_amount, pull_amount, notes "
                    "FROM gacha_income_items WHERE id='weapon-income'"
                )
            ).one() == (160, 2, "income notes")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.execute(
                text("SELECT version_num FROM schema_version")
            ).scalar_one() == (_REVISION_0003)

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM events WHERE id='event'"))
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT COUNT(*) FROM tasks WHERE id='task'")).scalar_one()
                == 0
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
