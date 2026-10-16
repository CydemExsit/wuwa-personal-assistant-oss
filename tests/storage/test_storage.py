from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import LargeBinary, inspect, text
from sqlalchemy.exc import IntegrityError

from wuwa_assistant.storage import (
    AppPaths,
    DatabaseCorruptionError,
    SqlAlchemyUnitOfWork,
    create_database,
    migrate_database,
)
from wuwa_assistant.storage.orm import REQUIRED_V1_TABLES, CharacterRecord, DataSetRecord


def test_fast_startup_revision_tracks_actual_migration_head() -> None:
    from alembic.script import ScriptDirectory

    from wuwa_assistant.storage.migration import _alembic_config
    from wuwa_assistant.storage.migrations import BUNDLED_SCHEMA_REVISION

    assert ScriptDirectory.from_config(_alembic_config()).get_heads() == [BUNDLED_SCHEMA_REVISION]


def test_current_database_reopens_without_running_migrations_but_checks_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wuwa_assistant.storage.database as database_module

    paths = AppPaths.from_root(tmp_path)
    with create_database(paths) as database:
        database.settings.set("preserved", {"value": 7})
    monkeypatch.setattr(
        database_module, "migrate_database", lambda *_: pytest.fail("unneeded migration")
    )
    checked = []
    inspect_health = database_module.inspect_database_health

    def track(*args, **kwargs):
        checked.append(True)
        return inspect_health(*args, **kwargs)

    monkeypatch.setattr(database_module, "inspect_database_health", track)
    with create_database(paths) as database:
        assert database.health.is_healthy
        assert database.settings.get("preserved") == {"value": 7}
    assert checked == [True]


def test_app_paths_keep_user_data_separated_and_support_windows_names(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "第二螢幕 使用者資料")

    assert paths.database_path == paths.root / "data" / "wuwa-personal-assistant.sqlite3"
    assert paths.bootstrap_path == paths.root / "config" / "bootstrap.json"
    assert paths.logs_dir == paths.root / "logs"
    assert paths.backups_dir == paths.root / "backups"
    assert paths.exports_dir == paths.root / "exports"
    assert paths.low_confidence_debug_dir == paths.root / "debug" / "low-confidence-rois"

    paths.ensure_created()
    assert paths.data_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.backups_dir.is_dir()
    assert paths.exports_dir.is_dir()
    assert paths.diagnostics_dir.is_dir()
    assert paths.restore_staging_dir.is_dir()
    assert paths.low_confidence_debug_dir.is_dir()
    assert not paths.database_path.exists()


def test_initial_migration_builds_complete_v1_schema_and_is_idempotent(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "db")
    with create_database(paths) as database:
        tables = set(inspect(database.engine).get_table_names())
        assert tables >= REQUIRED_V1_TABLES
        assert database.health.schema_revision == "0004_tasks_and_gacha_v1"
        assert database.health.journal_mode == "wal"
        assert database.health.foreign_keys_enabled is True

        migrate_database(database.engine, paths.database_path)
        with database.engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM schema_version")
            ).scalar_one() == ("0004_tasks_and_gacha_v1")


def test_unit_of_work_requires_explicit_commit_and_rolls_back(tmp_path: Path) -> None:
    with create_database(AppPaths.from_root(tmp_path / "transactions")) as database:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            assert unit.settings is not None
            unit.settings.set("capture.monitor_index", 1)

        assert database.settings.get("capture.monitor_index") is None

        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            assert unit.settings is not None
            unit.settings.set("capture.monitor_index", 2)
            unit.commit()

        assert database.settings.get("capture.monitor_index") == 2

        with (
            pytest.raises(RuntimeError, match="force rollback"),
            SqlAlchemyUnitOfWork(database.session_factory) as unit,
        ):
            assert unit.settings is not None
            unit.settings.set("capture.monitor_index", 3)
            raise RuntimeError("force rollback")

        assert database.settings.get("capture.monitor_index") == 2


def test_foreign_keys_are_enforced_on_every_session(tmp_path: Path) -> None:
    with create_database(AppPaths.from_root(tmp_path / "foreign keys")) as database:
        with pytest.raises(IntegrityError), database.session_factory.begin() as session:
            session.add(
                CharacterRecord(
                    data_set_id="missing-dataset",
                    name="不存在的資料集角色",
                )
            )

        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            session.add(CharacterRecord(data_set_id=data_set.id, name="測試角色"))

        with database.session_factory() as session:
            assert session.query(CharacterRecord).count() == 1


def test_settings_are_json_validated_and_persist_across_reopen(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "persistent settings")
    first = create_database(paths)
    first.settings.set(
        "ui.main_window",
        {"geometry_b64": "AAECAw==", "theme": "dark", "monitor": 1},
        value_schema_version=2,
    )
    first.dispose()

    second = create_database(paths)
    try:
        assert second.settings.get("ui.main_window") == {
            "geometry_b64": "AAECAw==",
            "theme": "dark",
            "monitor": 1,
        }
        stored = second.settings.list_all()[0]
        assert stored.value_schema_version == 2

        with pytest.raises(ValueError, match="finite JSON"):
            second.settings.set("invalid.bytes", b"not JSON")
        with pytest.raises(ValueError, match="finite JSON"):
            second.settings.set("invalid.nan", float("nan"))
    finally:
        second.dispose()


def test_corrupt_database_fails_friendly_without_overwriting_bytes(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "corrupt")
    paths.ensure_created()
    original = b"this is not a sqlite database and must be preserved"
    paths.database_path.write_bytes(original)

    with pytest.raises(DatabaseCorruptionError) as caught:
        create_database(paths)

    assert str(paths.database_path) in str(caught.value)
    assert "不會覆寫" in str(caught.value)
    assert paths.database_path.read_bytes() == original


def test_schema_has_no_binary_column_for_full_frame_persistence(tmp_path: Path) -> None:
    with create_database(AppPaths.from_root(tmp_path / "no frames")) as database:
        schema = inspect(database.engine)
        binary_columns = [
            f"{table}.{column['name']}"
            for table in schema.get_table_names()
            for column in schema.get_columns(table)
            if isinstance(column["type"], LargeBinary)
        ]
        assert binary_columns == []
