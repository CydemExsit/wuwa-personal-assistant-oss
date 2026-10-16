"""Pure conversion of public release-data tables into the small offline catalog."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .tacet_catalog import parse_tacet_catalog

SOURCE_REPOSITORY = "Arikatsu/WutheringWaves_Data"
SOURCE_PATHS = {
    "readme": "README.md",
    "fields": "BinData/adventure_detect/silentareadetection.json",
    "sets": "BinData/phantom/phantomfettergroup.json",
    "tabs": "BinData/adventure_detect/detectiontabtype.json",
    "text": "Textmaps/zh-Hant/multi_text/MultiText.json",
    "text1": "Textmaps/zh-Hant/multi_text_1sthalf/MultiText.json",
    "text2": "Textmaps/zh-Hant/multi_text_2ndhalf/MultiText.json",
}


def _rows(content: bytes, *, maximum: int) -> list[dict[str, Any]]:
    rows = json.loads(content)
    if (
        not isinstance(rows, list)
        or not 1 <= len(rows) <= maximum
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ValueError("公開遊戲資料的表格格式已改變。")
    return rows


def build_tacet_catalog_payload(
    sources: dict[str, bytes],
    *,
    revision: str,
    retrieved_on: str,
    previous_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Join identifiers; never infer set pairs from names, order or drop rates."""
    if set(sources) != set(SOURCE_PATHS):
        raise ValueError("無音區公開來源不完整。")
    readme = sources["readme"].decode("utf-8-sig")
    game = re.search(r"Game Version:\s*(\d+\.\d+\.\d+)", readme)
    resource = re.search(r"Resource Version:\s*(\d+\.\d+\.\d+)", readme)
    if not game or not resource or "Status: Release" not in readme:
        raise ValueError("來源不是已確認的正式遊戲版本。")
    if "Client region: Global" not in readme:
        raise ValueError("來源不是國際服資料。")
    texts: dict[str, str] = {}
    for key in ("text", "text1", "text2"):
        for row in _rows(sources[key], maximum=1_000_000):
            identifier, content = row.get("Id"), row.get("Content")
            if isinstance(identifier, str) and isinstance(content, str) and content:
                texts[identifier] = content
    sets: dict[int, str] = {}
    for row in _rows(sources["sets"], maximum=500):
        identifier, text_key = row.get("Id"), row.get("FetterGroupName")
        if type(identifier) is not int or identifier in sets or text_key not in texts:
            raise ValueError("合鳴套裝的名稱或識別碼無法確認。")
        sets[identifier] = texts[text_key]
    tabs = {row["Id"]: texts.get(row["Text"], "") for row in _rows(sources["tabs"], maximum=500)}
    fields: list[dict[str, Any]] = []
    source_url = f"https://github.com/{SOURCE_REPOSITORY}/blob/{revision}/{SOURCE_PATHS['fields']}"
    for row in _rows(sources["fields"], maximum=10_000):
        if row.get("Secondary") != 22:
            continue
        name = texts.get(row.get("Name", ""))
        region = tabs.get(row.get("DetectionTabType"))
        set_ids = row.get("PhantomFetterGroup")
        if (
            not name
            or not region
            or not isinstance(set_ids, list)
            or len(set_ids) != 2
            or any(type(value) is not int or value not in sets for value in set_ids)
        ):
            raise ValueError("無音區名稱、地區或双套裝資料不完整。")
        if row.get("BeginTimeStamp") != 0:
            raise ValueError("來源包含尚需確認開放時間的無音區。")
        fields.append(
            {
                "field_id": str(row["Id"]),
                "name": name,
                "name_key": row["Name"],
                "region": region,
                "set_names": [sets[value] for value in set_ids],
                "set_ids": set_ids,
                "map_id": row["MapId"],
                "mark_id": row["MarkId"],
                "source_url": source_url,
                "verified": True,
                "available_from": None,
            }
        )
    used_ids = {value for row in fields for value in row["set_ids"]}
    non_tacet = [name for key, name in sets.items() if key not in used_ids]
    all_names = set(sets.values())
    aliases = {key: value for key, value in (previous_aliases or {}).items() if value in all_names}
    note = (
        f"已核對國際服 {resource[1]} 靜態資料全部 {len(fields)} 個無音區的雙套裝; "
        f"查核日 {retrieved_on}。來源為公開社群擷取的正式版遊戲資料, "
        "並非官方掉率承諾。僅比較 COST 1/3 的套裝需求, 不推估掉率、詞條或保證提升; "
        "個人解鎖進度請在遊戲內確認。未列入無音區的套裝另提示來源待確認。"
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "game_version": resource[1],
        "client_version": game[1],
        "source_revision": revision,
        "retrieved_on": retrieved_on,
        "source_field_count": len(fields),
        "coverage_note": note,
        "source_type": "community_extracted_release_game_data",
        "source_tables": [
            {
                "name": key,
                "path": path,
                "sha256": hashlib.sha256(sources[key]).hexdigest(),
                "bytes": len(sources[key]),
                "url": f"https://raw.githubusercontent.com/{SOURCE_REPOSITORY}/{revision}/{path}",
            }
            for key, path in SOURCE_PATHS.items()
        ],
        "fields": fields,
        "set_registry": [{"id": key, "name": name} for key, name in sets.items()],
        "aliases": aliases,
        "non_tacet_sets": non_tacet,
    }
    parse_tacet_catalog(payload)
    return payload
