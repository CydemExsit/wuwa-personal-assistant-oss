from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wuwa_assistant.application.tacet_catalog import load_tacet_catalog
from wuwa_assistant.application.tacet_stock import TacetStockService
from wuwa_assistant.storage import AppPaths, create_database


def test_initial_stock_preset_and_reads_do_not_write_account_data(tmp_path: Path) -> None:
    database = create_database(AppPaths.from_root(tmp_path))
    try:
        catalog = load_tacet_catalog()
        service = TacetStockService(database.settings)
        before = database.settings.list_all()
        stocked = service.stocked_field_ids("primary", catalog)
        depleted = {f.field_id for f in catalog.fields} - stocked
        assert len(depleted) == 3
        for field in catalog.fields:
            expected = not ({13, 36, 37, 38} & set(field.set_ids))
            assert (field.field_id in stocked) is expected
        assert database.settings.list_all() == before
    finally:
        database.dispose()


def test_manual_stock_restarts_bulk_edits_and_datasets_stay_independent(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)
    database = create_database(paths)
    catalog = load_tacet_catalog()
    identifiers = tuple(field.field_id for field in catalog.fields)
    try:
        service = TacetStockService(database.settings)
        initial_other = service.stocked_field_ids("other", catalog)
        service.save("primary", catalog, identifiers, has_stock=True)
        assert service.stocked_field_ids("primary", catalog) == frozenset(identifiers)
        assert service.stocked_field_ids("other", catalog) == initial_other
        # One exhausted wanted drop restores the whole field, regardless of
        # plentiful other Echoes. No drop counts are requested or estimated.
        service.save("primary", catalog, (identifiers[0],), has_stock=False)
    finally:
        database.dispose()
    database = create_database(paths)
    try:
        service = TacetStockService(database.settings)
        assert service.stocked_field_ids("primary", catalog) == frozenset(identifiers[1:])
        service.save("primary", catalog, identifiers, has_stock=False)
        assert not service.stocked_field_ids("primary", catalog)
    finally:
        database.dispose()


def test_new_drops_and_corrupt_saved_rows_never_hide_farming_needs(tmp_path: Path) -> None:
    database = create_database(AppPaths.from_root(tmp_path))
    try:
        catalog = load_tacet_catalog()
        old = catalog.fields[0]
        service = TacetStockService(database.settings)
        service.save("primary", catalog, (old.field_id,), has_stock=True)
        fresh = replace(old, field_id="999999", set_ids=(900, 901), set_names=("新A", "新B"))
        updated = replace(old, set_ids=(old.set_ids[0], 900), set_names=(old.set_names[0], "新A"))
        expanded = replace(catalog, fields=(updated, *catalog.fields[1:], fresh))
        stocked = service.stocked_field_ids("primary", expanded)
        assert fresh.field_id not in stocked
        assert old.field_id not in stocked
        service.save("primary", expanded, (fresh.field_id,), has_stock=True)
        assert fresh.field_id in service.stocked_field_ids("primary", expanded)
        key = "planning/tacet_stock.v1/primary"
        database.settings.set(key, {old.field_id: {"sets": sorted(old.set_names), "has_stock": 1}})
        assert old.field_id not in service.stocked_field_ids("primary", catalog)
        before = database.settings.get(key)
        with pytest.raises(ValueError, match="不存在"):
            service.save("primary", catalog, (old.field_id, "unknown"), has_stock=False)
        assert database.settings.get(key) == before
    finally:
        database.dispose()
