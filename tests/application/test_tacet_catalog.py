from __future__ import annotations

import json
from pathlib import Path

import pytest

from wuwa_assistant.application.tacet_catalog import (
    CATALOG_NAME,
    bundled_catalog_payload,
    canonical_set_name,
    load_tacet_catalog,
    parse_tacet_catalog,
)
from wuwa_assistant.application.tacet_source import SOURCE_PATHS, build_tacet_catalog_payload
from wuwa_assistant.application.tacet_sync import BRANCH_URL, TacetCatalogSync


def _sources() -> dict[str, bytes]:
    rows = {
        "fields": [
            {
                "Id": 1,
                "Secondary": 22,
                "Name": "zone",
                "DetectionTabType": 1,
                "PhantomFetterGroup": [1, 2],
                "BeginTimeStamp": 0,
                "MapId": 8,
                "MarkId": 9,
            }
        ],
        "sets": [{"Id": 1, "FetterGroupName": "set1"}, {"Id": 2, "FetterGroupName": "set2"}],
        "tabs": [{"Id": 1, "Text": "region"}],
        "text": [{"Id": "zone", "Content": "無音區"}, {"Id": "region", "Content": "地區"}],
        "text1": [{"Id": "set1", "Content": "套裝甲"}],
        "text2": [{"Id": "set2", "Content": "套裝乙"}],
    }
    result = {key: json.dumps(value).encode() for key, value in rows.items()}
    result["readme"] = (
        b"Game Version: 3.8.0\nResource Version: 3.8.1\nStatus: Release\nClient region: Global"
    )
    return result


def test_complete_bundled_catalog_and_aliases() -> None:
    catalog = load_tacet_catalog()
    assert len(catalog.fields) == 21
    assert len({field.field_id for field in catalog.fields}) == 21
    assert all(len(field.set_names) == 2 for field in catalog.fields)
    assert canonical_set_name("碎夢亡鬼之魔") == "碎夢亡鬼之魘"
    assert next(field for field in catalog.fields if field.name == "燼心域無音區").set_ids == (
        36,
        37,
    )


def test_reject_partial_catalog_and_fall_back_from_corrupt_cache(tmp_path: Path) -> None:
    payload = bundled_catalog_payload()
    payload["fields"].pop()
    with pytest.raises(ValueError, match="不完整"):
        parse_tacet_catalog(payload)
    (tmp_path / CATALOG_NAME).write_text(json.dumps(payload), encoding="utf-8")
    assert len(load_tacet_catalog(tmp_path).fields) == 21


def test_source_join_rejects_unknown_pair_and_unreleased_field() -> None:
    sources = _sources()
    valid = build_tacet_catalog_payload(sources, revision="a" * 40, retrieved_on="2026-10-09")
    assert valid["fields"][0]["set_names"] == ["套裝甲", "套裝乙"]
    rows = json.loads(sources["fields"])
    rows[0]["BeginTimeStamp"] = 123
    sources["fields"] = json.dumps(rows).encode()
    with pytest.raises(ValueError, match="開放時間"):
        build_tacet_catalog_payload(sources, revision="a" * 40, retrieved_on="2026-10-09")
    rows[0]["BeginTimeStamp"] = 0
    rows[0]["PhantomFetterGroup"] = [1, 999]
    sources["fields"] = json.dumps(rows).encode()
    with pytest.raises(ValueError, match="不完整"):
        build_tacet_catalog_payload(sources, revision="a" * 40, retrieved_on="2026-10-09")


def test_sync_checks_revision_before_large_downloads(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str, maximum: int) -> bytes:
        calls.append(url)
        assert maximum <= 128 * 1024
        return json.dumps({"commit": {"sha": load_tacet_catalog().source_revision}}).encode()

    assert "目前版本" in TacetCatalogSync(tmp_path, fetch=fetch).synchronize()
    assert calls == [BRANCH_URL]
    assert not (tmp_path / CATALOG_NAME).exists()


def test_atomic_new_version_then_offline_retains_known_good(tmp_path: Path) -> None:
    sources = _sources()

    def fetch(url: str, maximum: int) -> bytes:
        del maximum
        if url == BRANCH_URL:
            return json.dumps({"commit": {"sha": "a" * 40}}).encode()
        return next(sources[key] for key, path in SOURCE_PATHS.items() if url.endswith(path))

    assert "已更新" in TacetCatalogSync(tmp_path, fetch=fetch).synchronize()
    before = (tmp_path / CATALOG_NAME).read_bytes()
    assert load_tacet_catalog(tmp_path).game_version == "3.8.1"

    def offline(url: str, maximum: int) -> bytes:
        raise OSError("offline")

    assert "保留 3.8.1" in TacetCatalogSync(tmp_path, fetch=offline).synchronize()
    assert (tmp_path / CATALOG_NAME).read_bytes() == before
