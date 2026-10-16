"""Scope material codes to one local data set.

Revision ID: 0003_material_codes_per_dataset
Revises: 0002_scoring_provenance
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_material_codes_per_dataset"
down_revision: str | None = "0002_scoring_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVENTORY_BACKUP = "_m0003_inventory_counts"
_TARGET_MATERIAL_BACKUP = "_m0003_target_materials"
_DAILY_PLAN_BACKUP = "_m0003_daily_plan_materials"


def _drop_temp_tables() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_INVENTORY_BACKUP}")
    op.execute(f"DROP TABLE IF EXISTS {_TARGET_MATERIAL_BACKUP}")
    op.execute(f"DROP TABLE IF EXISTS {_DAILY_PLAN_BACKUP}")


def _detach_material_references() -> None:
    _drop_temp_tables()
    op.execute(
        f"CREATE TEMPORARY TABLE {_INVENTORY_BACKUP} AS "
        "SELECT id, data_set_id, material_id, quantity, updated_at FROM inventory_counts"
    )
    op.execute(
        f"CREATE TEMPORARY TABLE {_TARGET_MATERIAL_BACKUP} AS "
        "SELECT cultivation_target_id, material_id, required_quantity "
        "FROM cultivation_target_materials"
    )
    op.execute(
        f"CREATE TEMPORARY TABLE {_DAILY_PLAN_BACKUP} AS "
        "SELECT id, material_id FROM daily_plans WHERE material_id IS NOT NULL"
    )
    op.execute("DELETE FROM inventory_counts")
    op.execute("DELETE FROM cultivation_target_materials")
    op.execute("UPDATE daily_plans SET material_id = NULL WHERE material_id IS NOT NULL")


def _restore_material_references() -> None:
    op.execute(
        "INSERT INTO inventory_counts (id, data_set_id, material_id, quantity, updated_at) "
        f"SELECT id, data_set_id, material_id, quantity, updated_at FROM {_INVENTORY_BACKUP}"
    )
    op.execute(
        "INSERT INTO cultivation_target_materials "
        "(cultivation_target_id, material_id, required_quantity) "
        "SELECT cultivation_target_id, material_id, required_quantity "
        f"FROM {_TARGET_MATERIAL_BACKUP}"
    )
    op.execute(
        "UPDATE daily_plans SET material_id = ("
        f"SELECT backup.material_id FROM {_DAILY_PLAN_BACKUP} AS backup "
        "WHERE backup.id = daily_plans.id) "
        f"WHERE id IN (SELECT id FROM {_DAILY_PLAN_BACKUP})"
    )
    _drop_temp_tables()


def _assert_foreign_keys() -> None:
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(f"foreign key violations after material migration: {violations!r}")


def upgrade() -> None:
    null_count: int = (
        op.get_bind()
        .exec_driver_sql("SELECT COUNT(*) FROM materials WHERE data_set_id IS NULL")
        .scalar_one()
    )
    if null_count:
        raise RuntimeError(
            "cannot scope material codes: legacy materials without a data_set_id exist"
        )
    try:
        _detach_material_references()
        with op.batch_alter_table("materials") as batch:
            batch.drop_constraint("uq_materials_code", type_="unique")
            batch.alter_column("data_set_id", nullable=False)
            batch.create_unique_constraint(
                "uq_materials_dataset_code",
                ["data_set_id", "code"],
            )
        _restore_material_references()
        _assert_foreign_keys()
    except Exception:
        _drop_temp_tables()
        raise


def downgrade() -> None:
    duplicate = (
        op.get_bind()
        .exec_driver_sql("SELECT code FROM materials GROUP BY code HAVING COUNT(*) > 1 LIMIT 1")
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade material code scope while duplicate codes exist across data sets"
        )
    try:
        _detach_material_references()
        with op.batch_alter_table("materials") as batch:
            batch.drop_constraint("uq_materials_dataset_code", type_="unique")
            batch.alter_column("data_set_id", nullable=True)
            batch.create_unique_constraint("uq_materials_code", ["code"])
        _restore_material_references()
        _assert_foreign_keys()
    except Exception:
        _drop_temp_tables()
        raise
