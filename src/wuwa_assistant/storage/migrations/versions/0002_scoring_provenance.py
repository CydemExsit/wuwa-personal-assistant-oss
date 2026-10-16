"""Add durable scoring decision identity and score provenance.

Revision ID: 0002_scoring_provenance
Revises: 0001_v1
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "0002_scoring_provenance"
down_revision: str | None = "0001_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ECHO_SESSION_LINKS = "_migration_0002_echo_session_links"


def _legacy_fingerprint(decision_id: str) -> str:
    payload = f"legacy-upgrade-decision:{decision_id}".encode()
    return sha256(payload).hexdigest()


def _capture_echo_session_links() -> None:
    op.execute(
        f"CREATE TEMPORARY TABLE {_ECHO_SESSION_LINKS} AS "
        "SELECT id AS echo_session_id, echo_id FROM echo_sessions "
        "WHERE echo_id IS NOT NULL"
    )


def _restore_echo_session_links() -> None:
    op.execute(
        "UPDATE echo_sessions SET echo_id = ("
        f"SELECT links.echo_id FROM {_ECHO_SESSION_LINKS} AS links "
        "WHERE links.echo_session_id = echo_sessions.id"
        f") WHERE id IN (SELECT echo_session_id FROM {_ECHO_SESSION_LINKS})"
    )
    op.execute(f"DROP TABLE {_ECHO_SESSION_LINKS}")


def upgrade() -> None:
    with op.batch_alter_table("upgrade_decisions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("evaluation_fingerprint", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "profile_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "observation_payload_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "requires_manual_confirmation",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "decision_inputs_json",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "scoring_context_json",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )

    connection = op.get_bind()
    legacy_query = sa.text(
        "SELECT id FROM upgrade_decisions WHERE evaluation_fingerprint IS NULL ORDER BY id"
    )
    legacy_ids: list[str] = list(map(str, connection.execute(legacy_query).scalars()))
    for decision_id in legacy_ids:
        connection.execute(
            sa.text(
                "UPDATE upgrade_decisions SET evaluation_fingerprint = :fingerprint "
                "WHERE id = :decision_id"
            ),
            {
                "decision_id": decision_id,
                "fingerprint": _legacy_fingerprint(decision_id),
            },
        )

    with op.batch_alter_table("upgrade_decisions", schema=None) as batch_op:
        batch_op.alter_column(
            "evaluation_fingerprint",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "profile_version",
            existing_type=sa.Integer(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "observation_payload_version",
            existing_type=sa.Integer(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "requires_manual_confirmation",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "decision_inputs_json",
            existing_type=sa.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "scoring_context_json",
            existing_type=sa.JSON(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_upgrade_decisions_evaluation_fingerprint",
            ["evaluation_fingerprint"],
        )

    preserve_echo_session_links = connection.dialect.name == "sqlite"
    if preserve_echo_session_links:
        # SQLite batch mode drops the old echoes table, which fires the existing
        # echo_sessions ON DELETE SET NULL action. Preserve and restore those links.
        _capture_echo_session_links()
    with op.batch_alter_table("echoes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("score_profile_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("score_profile_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("score_observation_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_echoes_score_profile_id_scoring_profiles"),
            "scoring_profiles",
            ["score_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            op.f("fk_echoes_score_observation_id_echo_observations"),
            "echo_observations",
            ["score_observation_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            op.f("ix_echoes_score_profile_id"),
            ["score_profile_id"],
            unique=False,
        )
        batch_op.create_index(
            op.f("ix_echoes_score_observation_id"),
            ["score_observation_id"],
            unique=False,
        )
    if preserve_echo_session_links:
        _restore_echo_session_links()


def downgrade() -> None:
    connection = op.get_bind()
    preserve_echo_session_links = connection.dialect.name == "sqlite"
    if preserve_echo_session_links:
        _capture_echo_session_links()
    with op.batch_alter_table("echoes", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_echoes_score_observation_id"))
        batch_op.drop_index(op.f("ix_echoes_score_profile_id"))
        batch_op.drop_constraint(
            op.f("fk_echoes_score_observation_id_echo_observations"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("fk_echoes_score_profile_id_scoring_profiles"),
            type_="foreignkey",
        )
        batch_op.drop_column("score_observation_id")
        batch_op.drop_column("score_profile_version")
        batch_op.drop_column("score_profile_id")
    if preserve_echo_session_links:
        _restore_echo_session_links()

    with op.batch_alter_table("upgrade_decisions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_upgrade_decisions_evaluation_fingerprint",
            type_="unique",
        )
        batch_op.drop_column("scoring_context_json")
        batch_op.drop_column("decision_inputs_json")
        batch_op.drop_column("requires_manual_confirmation")
        batch_op.drop_column("observation_payload_version")
        batch_op.drop_column("profile_version")
        batch_op.drop_column("evaluation_fingerprint")
