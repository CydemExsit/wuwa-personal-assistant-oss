from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, MetaData, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.engine import create_sqlite_engine
from wuwa_assistant.storage.migration import _alembic_config
from wuwa_assistant.storage.orm import EchoRecord, UpgradeDecisionRecord

_REVISION_0001 = "0001_v1"
_REVISION_0002 = "0002_scoring_provenance"
_REVISION_HEAD = "0004_tasks_and_gacha_v1"
_NOW = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
_DATA_SET_ID = "legacy-data-set"
_PROFILE_ID = "legacy-profile"
_CAPTURE_ID = "legacy-capture"
_ECHO_ID = "legacy-echo"
_ECHO_SESSION_ID = "legacy-echo-session"
_OBSERVATION_ID = "legacy-observation"
_DECISION_IDS = ("legacy-decision-1", "legacy-decision-2")


def _run_upgrade(engine: Engine, revision: str) -> None:
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _run_downgrade(engine: Engine, revision: str) -> None:
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def _reflected_tables(connection: Connection) -> dict[str, Table]:
    names = (
        "data_sets",
        "scoring_profiles",
        "capture_sessions",
        "echoes",
        "echo_sessions",
        "echo_observations",
        "upgrade_decisions",
    )
    metadata = MetaData()
    metadata.reflect(bind=connection, only=names)
    return {name: metadata.tables[name] for name in names}


def _seed_legacy_graph(engine: Engine) -> None:
    with engine.begin() as connection:
        tables = _reflected_tables(connection)
        connection.execute(
            tables["data_sets"]
            .insert()
            .values(
                id=_DATA_SET_ID,
                name="Legacy primary",
                kind="primary",
                is_active=True,
                created_at=_NOW,
            )
        )
        connection.execute(
            tables["scoring_profiles"]
            .insert()
            .values(
                id=_PROFILE_ID,
                data_set_id=_DATA_SET_ID,
                name="Legacy profile",
                template_kind="custom",
                usable_threshold=55.0,
                passing_threshold=70.0,
                perfect_threshold=88.0,
                exceptional_threshold=95.0,
                threshold_rules_json=[],
                is_system_template=False,
                is_default=True,
                version=7,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        connection.execute(
            tables["capture_sessions"]
            .insert()
            .values(
                id=_CAPTURE_ID,
                data_set_id=_DATA_SET_ID,
                scoring_profile_id=_PROFILE_ID,
                backend_kind="legacy-test",
                monitor_index=0,
                sampling_hz=5.0,
                stable_wait_ms=600,
                status="stopped",
                started_at=_NOW,
                app_version="0.4.0",
                metadata_json={},
            )
        )
        connection.execute(
            tables["echoes"]
            .insert()
            .values(
                id=_ECHO_ID,
                data_set_id=_DATA_SET_ID,
                name="Legacy Echo",
                main_stats_json=[],
                sub_stats_json=[],
                final_status="keep",
                score=81.25,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        connection.execute(
            tables["echo_sessions"]
            .insert()
            .values(
                id=_ECHO_SESSION_ID,
                capture_session_id=_CAPTURE_ID,
                echo_id=_ECHO_ID,
                local_session_id="legacy-local-session",
                current_state="complete",
                is_discarded=False,
                was_consumed=False,
                started_at=_NOW,
                metadata_json={},
            )
        )
        connection.execute(
            tables["echo_observations"]
            .insert()
            .values(
                id=_OBSERVATION_ID,
                echo_session_id=_ECHO_SESSION_ID,
                sequence_no=1,
                page_type="echo_enhancement",
                main_stats_json=[],
                projected_main_stats_json=[],
                sub_stats_json=[],
                confidence_json={},
                raw_payload_json={"source": "legacy"},
                effective_payload_json={"source": "legacy"},
                payload_version=1,
                requires_review=False,
                observed_at=_NOW,
            )
        )
        connection.execute(
            tables["upgrade_decisions"].insert(),
            [
                {
                    "id": decision_id,
                    "echo_session_id": _ECHO_SESSION_ID,
                    "observation_id": _OBSERVATION_ID,
                    "scoring_profile_id": _PROFILE_ID,
                    "decision": "keep",
                    "probabilities_json": {},
                    "breakdown_json": {"legacy": index},
                    "assumptions_version": "legacy-v1",
                    "created_at": _NOW,
                }
                for index, decision_id in enumerate(_DECISION_IDS, start=1)
            ],
        )


def _schema_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM schema_version")).scalar_one())


def _legacy_fingerprint(decision_id: str) -> str:
    return sha256(f"legacy-upgrade-decision:{decision_id}".encode()).hexdigest()


def test_fresh_database_has_scoring_identity_and_provenance_constraints(
    tmp_path: Path,
) -> None:
    with create_database(AppPaths.from_root(tmp_path / "fresh-0002")) as database:
        schema = inspect(database.engine)
        decision_columns = {
            column["name"]: column for column in schema.get_columns("upgrade_decisions")
        }
        echo_columns = {column["name"]: column for column in schema.get_columns("echoes")}

        assert set(decision_columns) >= {
            "evaluation_fingerprint",
            "profile_version",
            "observation_payload_version",
            "requires_manual_confirmation",
            "decision_inputs_json",
            "scoring_context_json",
        }
        assert all(
            not decision_columns[name]["nullable"]
            for name in (
                "evaluation_fingerprint",
                "profile_version",
                "observation_payload_version",
                "requires_manual_confirmation",
                "decision_inputs_json",
                "scoring_context_json",
            )
        )
        assert all(
            decision_columns[name]["default"] is None
            for name in (
                "profile_version",
                "observation_payload_version",
                "requires_manual_confirmation",
                "decision_inputs_json",
                "scoring_context_json",
            )
        )
        assert all(
            echo_columns[name]["nullable"]
            for name in (
                "score_profile_id",
                "score_profile_version",
                "score_observation_id",
            )
        )

        unique_columns = {
            tuple(constraint["column_names"])
            for constraint in schema.get_unique_constraints("upgrade_decisions")
        }
        assert ("evaluation_fingerprint",) in unique_columns

        echo_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key
            for foreign_key in schema.get_foreign_keys("echoes")
        }
        assert echo_foreign_keys[("score_profile_id",)]["referred_table"] == ("scoring_profiles")
        assert echo_foreign_keys[("score_profile_id",)]["options"]["ondelete"] == "SET NULL"
        assert echo_foreign_keys[("score_observation_id",)]["referred_table"] == (
            "echo_observations"
        )
        assert echo_foreign_keys[("score_observation_id",)]["options"]["ondelete"] == "SET NULL"
        assert {index["name"] for index in schema.get_indexes("echoes")} >= {
            "ix_echoes_score_profile_id",
            "ix_echoes_score_observation_id",
        }


def test_upgrade_from_0001_backfills_legacy_rows_and_enforces_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade-from-0001.sqlite3"
    engine = create_sqlite_engine(database_path)
    try:
        _run_upgrade(engine, _REVISION_0001)
        _seed_legacy_graph(engine)
        _run_upgrade(engine, "head")

        assert _schema_revision(engine) == _REVISION_HEAD
        with Session(engine) as session:
            decisions = list(
                session.scalars(
                    select(UpgradeDecisionRecord).order_by(UpgradeDecisionRecord.id)
                ).all()
            )
            echo = session.get(EchoRecord, _ECHO_ID)

        assert [decision.evaluation_fingerprint for decision in decisions] == [
            _legacy_fingerprint(decision_id) for decision_id in _DECISION_IDS
        ]
        assert all(decision.profile_version == 1 for decision in decisions)
        assert all(decision.observation_payload_version == 1 for decision in decisions)
        assert all(not decision.requires_manual_confirmation for decision in decisions)
        assert all(decision.decision_inputs_json == {} for decision in decisions)
        assert all(decision.scoring_context_json == {} for decision in decisions)
        assert echo is not None
        assert echo.score == 81.25
        assert echo.score_profile_id is None
        assert echo.score_profile_version is None
        assert echo.score_observation_id is None

        current_fingerprint = "f" * 64
        with Session(engine) as session, session.begin():
            stored_echo = session.get(EchoRecord, _ECHO_ID)
            assert stored_echo is not None
            stored_echo.score_profile_id = _PROFILE_ID
            stored_echo.score_profile_version = 7
            stored_echo.score_observation_id = _OBSERVATION_ID
            session.add(
                UpgradeDecisionRecord(
                    echo_session_id=_ECHO_SESSION_ID,
                    observation_id=_OBSERVATION_ID,
                    scoring_profile_id=_PROFILE_ID,
                    evaluation_fingerprint=current_fingerprint,
                    profile_version=7,
                    observation_payload_version=1,
                    decision="manual_confirmation",
                    probabilities_json={"passing": 0.5},
                    breakdown_json={"score": 81.25},
                    requires_manual_confirmation=True,
                    decision_inputs_json={"level": 20},
                    scoring_context_json={"current_totals": {"crit_rate": 65.0}},
                    assumptions_version="current-v1",
                    reason="test current schema",
                )
            )

        with Session(engine) as session:
            current = session.scalar(
                select(UpgradeDecisionRecord).where(
                    UpgradeDecisionRecord.evaluation_fingerprint == current_fingerprint
                )
            )
            stored_echo = session.get(EchoRecord, _ECHO_ID)
        assert current is not None
        assert current.profile_version == 7
        assert current.observation_payload_version == 1
        assert current.requires_manual_confirmation
        assert current.decision_inputs_json == {"level": 20}
        assert current.scoring_context_json == {"current_totals": {"crit_rate": 65.0}}
        assert stored_echo is not None
        assert stored_echo.score_profile_id == _PROFILE_ID
        assert stored_echo.score_profile_version == 7
        assert stored_echo.score_observation_id == _OBSERVATION_ID

        with engine.connect() as connection:
            linked_echo_id = connection.execute(
                text("SELECT echo_id FROM echo_sessions WHERE id = :session_id"),
                {"session_id": _ECHO_SESSION_ID},
            ).scalar_one()
        assert linked_echo_id == _ECHO_ID

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE upgrade_decisions SET evaluation_fingerprint = :fingerprint "
                    "WHERE id = :decision_id"
                ),
                {
                    "decision_id": _DECISION_IDS[1],
                    "fingerprint": _legacy_fingerprint(_DECISION_IDS[0]),
                },
            )

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM upgrade_decisions"))
            connection.execute(
                text("DELETE FROM echo_observations WHERE id = :observation_id"),
                {"observation_id": _OBSERVATION_ID},
            )
            connection.execute(
                text("DELETE FROM scoring_profiles WHERE id = :profile_id"),
                {"profile_id": _PROFILE_ID},
            )
            provenance = connection.execute(
                text(
                    "SELECT score_profile_id, score_profile_version, score_observation_id "
                    "FROM echoes WHERE id = :echo_id"
                ),
                {"echo_id": _ECHO_ID},
            ).one()
        assert tuple(provenance) == (None, 7, None)
    finally:
        engine.dispose()


def test_0002_downgrade_preserves_legacy_rows_and_can_upgrade_again(tmp_path: Path) -> None:
    database_path = tmp_path / "round-trip-0002.sqlite3"
    engine = create_sqlite_engine(database_path)
    try:
        _run_upgrade(engine, _REVISION_0001)
        _seed_legacy_graph(engine)
        _run_upgrade(engine, _REVISION_0002)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE echoes SET score_profile_id = :profile_id, "
                    "score_profile_version = 7, score_observation_id = :observation_id "
                    "WHERE id = :echo_id"
                ),
                {
                    "echo_id": _ECHO_ID,
                    "profile_id": _PROFILE_ID,
                    "observation_id": _OBSERVATION_ID,
                },
            )

        _run_downgrade(engine, _REVISION_0001)

        assert _schema_revision(engine) == _REVISION_0001
        schema = inspect(engine)
        assert {
            "score_profile_id",
            "score_profile_version",
            "score_observation_id",
        }.isdisjoint(column["name"] for column in schema.get_columns("echoes"))
        assert {
            "evaluation_fingerprint",
            "profile_version",
            "observation_payload_version",
            "requires_manual_confirmation",
            "decision_inputs_json",
            "scoring_context_json",
        }.isdisjoint(column["name"] for column in schema.get_columns("upgrade_decisions"))
        assert "uq_echoes_active_character_slot" in {
            index["name"] for index in schema.get_indexes("echoes")
        }
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT score FROM echoes WHERE id = :echo_id"),
                    {"echo_id": _ECHO_ID},
                ).scalar_one()
                == 81.25
            )
            assert (
                connection.execute(
                    text("SELECT echo_id FROM echo_sessions WHERE id = :session_id"),
                    {"session_id": _ECHO_SESSION_ID},
                ).scalar_one()
                == _ECHO_ID
            )
            assert connection.execute(
                text("SELECT COUNT(*) FROM upgrade_decisions")
            ).scalar_one() == len(_DECISION_IDS)

        _run_upgrade(engine, _REVISION_0002)

        assert _schema_revision(engine) == _REVISION_0002
        with Session(engine) as session:
            fingerprints = list(
                session.scalars(
                    select(UpgradeDecisionRecord.evaluation_fingerprint).order_by(
                        UpgradeDecisionRecord.id
                    )
                ).all()
            )
        assert fingerprints == [_legacy_fingerprint(decision_id) for decision_id in _DECISION_IDS]
    finally:
        engine.dispose()
