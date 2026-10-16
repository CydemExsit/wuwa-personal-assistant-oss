from __future__ import annotations

from pathlib import Path

import pytest

from wuwa_assistant.application.character_catalog import (
    load_bundled_character_catalog,
    parse_character_catalog,
)
from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord


def test_bundled_catalog_is_versioned_unique_and_does_not_claim_account_or_tier_data() -> None:
    catalog = load_bundled_character_catalog()

    assert catalog.schema_version == 1
    assert catalog.locale == "zh-TW"
    assert catalog.entries
    assert len(catalog.entries) == len({entry.identifier for entry in catalog.entries})
    assert len(catalog.entries) == len({entry.name.casefold() for entry in catalog.entries})
    assert "不代表帳號擁有" in catalog.disclaimer
    assert "Tier" in catalog.disclaimer
    assert catalog.search("緋雪")[0].name == "緋雪"
    assert all(source.url.startswith("https://") for source in catalog.sources)


def test_catalog_parser_rejects_unversioned_content() -> None:
    with pytest.raises(ValueError, match="schema version"):
        parse_character_catalog(
            {
                "schema_version": 2,
                "catalog_version": "future",
                "as_of_date": "2026-08-20",
                "locale": "zh-TW",
                "source_status": "unknown",
                "disclaimer": "test",
                "sources": [],
                "entries": [],
            }
        )


def test_batch_create_is_atomic_and_skips_existing_including_archived(tmp_path: Path) -> None:
    database = create_database(AppPaths.from_root(tmp_path / "catalog-batch"))
    try:
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            data_set_id = data_set.id
        service = CharacterCultivationService(database.session_factory)
        existing = service.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter(name="緋雪"),
        )
        service.archive_character(existing.summary.id, data_set_id=data_set_id)

        result = service.batch_create_characters(
            data_set_id=data_set_id,
            commands=(
                CreateCharacter(name="緋雪"),
                CreateCharacter(name="愛彌斯", element="衍射"),
            ),
        )

        assert tuple(item.summary.name for item in result.created) == ("愛彌斯",)
        assert result.skipped_names == ("緋雪",)
        assert {
            item.name
            for item in service.list_characters(
                data_set_id=data_set_id,
                include_archived=True,
            )
        } == {
            "緋雪",
            "愛彌斯",
        }

        with pytest.raises(ValueError, match="must be unique"):
            service.batch_create_characters(
                data_set_id=data_set_id,
                commands=(CreateCharacter(name="卡提希婭"), CreateCharacter(name="卡提希婭")),
            )
        assert "卡提希婭" not in {
            item.name
            for item in service.list_characters(
                data_set_id=data_set_id,
                include_archived=True,
            )
        }
    finally:
        database.dispose()
