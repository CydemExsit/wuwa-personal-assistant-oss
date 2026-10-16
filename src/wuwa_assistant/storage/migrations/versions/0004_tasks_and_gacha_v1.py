"""Harden task retention and gacha planning fields for V1.

Revision ID: 0004_tasks_and_gacha_v1
Revises: 0003_material_codes_per_dataset
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_tasks_and_gacha_v1"
down_revision: str | None = "0003_material_codes_per_dataset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DATE_INDEXES = (
    ("ix_events_starts_at", "events", "starts_at"),
    ("ix_tasks_snoozed_until", "tasks", "snoozed_until"),
    ("ix_tasks_completed_at", "tasks", "completed_at"),
    ("ix_gacha_targets_target_date", "gacha_targets", "target_date"),
    (
        "ix_gacha_income_items_expected_date",
        "gacha_income_items",
        "expected_date",
    ),
)


def _replace_task_event_foreign_key(*, ondelete: str) -> None:
    with op.batch_alter_table("tasks", schema=None) as batch:
        batch.drop_constraint("fk_tasks_event_id_events", type_="foreignkey")
        batch.create_foreign_key(
            "fk_tasks_event_id_events",
            "events",
            ["event_id"],
            ["id"],
            ondelete=ondelete,
        )


def _assert_foreign_keys() -> None:
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(f"foreign key violations after tasks/gacha migration: {violations!r}")


def upgrade() -> None:
    _replace_task_event_foreign_key(ondelete="SET NULL")

    with op.batch_alter_table("gacha_income_items", schema=None) as batch:
        batch.add_column(
            sa.Column(
                "pull_type",
                sa.String(length=20),
                server_default=sa.text("'unknown'"),
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_gacha_income_items_pull_type",
            "pull_type IN ('character', 'weapon', 'unknown')",
        )

    for name, table, column in _DATE_INDEXES:
        op.create_index(name, table, [column], unique=False)

    _assert_foreign_keys()


def downgrade() -> None:
    for name, table, _column in reversed(_DATE_INDEXES):
        op.drop_index(name, table_name=table)

    with op.batch_alter_table("gacha_income_items", schema=None) as batch:
        batch.drop_constraint("ck_gacha_income_items_pull_type", type_="check")
        batch.drop_column("pull_type")

    _replace_task_event_foreign_key(ondelete="CASCADE")
    _assert_foreign_keys()
