"""Compile the small offline acquisition index from a pinned Global Release export.

Pass an extracted Arikatsu/WutheringWaves_Data tree (see docs/echo-demand.md).
The output contains navigation and drop facts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

REVISION = "9218d612ad815e398e064e577e42aaf878899968"
BASE_URL = f"https://github.com/Arikatsu/WutheringWaves_Data/blob/{REVISION}/"
TABLES = {
    "echoes": "BinData/phantom/phantomitem.json",
    "sets": "BinData/phantom/phantomfettergroup.json",
    "enemies": "BinData/adventure_detect/monsterdetection.json",
    "activities": "BinData/adventure_detect/silentareadetection.json",
    "dungeons": "BinData/adventure_detect/dungeondetection.json",
    "entrances": "BinData/instance_dungeon/instancedungeonentrance.json",
    "regions": "BinData/adventure_detect/detectiontabtype.json",
    "markers": "BinData/map_mark/mapmark.json",
}
TEXT_FOLDERS = ("multi_text", "multi_text_1sthalf", "multi_text_2ndhalf")
# Documented cost classes: common, elite, overlord, calamity.
COST = {0: 1, 1: 3, 2: 4, 3: 4}
# Echo and enemy names differ for these documented encounters. Use released IDs.
ENCOUNTER_ECHO_IDS = {
    340000120: (60001065,),  # Fleurdelys
    340000150: (60001165,),  # Fenrico
    340000200: (60001675,),  # Leviathan
    340000280: (60001995, 60002005),  # Denia's two distinct Echoes
    340000290: (60002015,),  # Nightmare: Adam Smasher
    340000330: (60002255,),  # Suhsin the Inevitable
}
LOCATIONS = {
    330000030: "瑝瓏 · 荒石高地",
    330000040: "瑝瓏 · 虎口礦場",
    340000070: "瑝瓏 · 北落野",
    340000110: "七丘 · 哀慟谷 · 湖心島",
    340000130: "黎那汐塔 · 貝奧海域",
    340000020: "瑝瓏 · 雲陵谷",
    340000330: "夢州 · 夢玄溪山",
}


def compile_catalog(root: Path) -> dict[str, Any]:
    metadata = (root / "README.md").read_text(encoding="utf-8-sig")
    for expected in (
        "Game Version: 3.7.0",
        "Resource Version: 3.7.8",
        "Status: Release",
        "Client region: Global",
    ):
        if expected not in metadata:
            raise ValueError(f"Expected pinned public data: {expected}")
    provenance = {}

    def read(relative: str) -> list[dict[str, Any]]:
        data = (root / relative).read_bytes()
        provenance[relative] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    text = {
        row["Id"]: row["Content"]
        for folder in TEXT_FOLDERS
        for row in read(f"Textmaps/zh-Hant/{folder}/MultiText.json")
    }
    tables = {key: read(path) for key, path in TABLES.items()}
    names = {row["Id"]: text[row["FetterGroupName"]] for row in tables["sets"]}
    regions = {row["Id"]: text[row["Text"]] for row in tables["regions"]}
    markers = {row["MarkId"]: row for row in tables["markers"]}
    weekly = {row["DungeonId"]: row for row in tables["dungeons"] if row["Secondary"] == 7}
    entrances = {row["MarkId"]: row["Id"] for row in tables["entrances"] if row["Id"] in weekly}
    items = {
        row["ItemId"]: row
        for row in tables["echoes"]
        if row["QualityId"] == 5
        and row["PhantomType"] == 1
        and row["ParentMonsterId"] == 0
        and row["ObtainedShow"] == 1
    }
    by_name = defaultdict(list)
    for row in items.values():
        by_name[row["MonsterName"]].append(row)

    def pairs(echoes: list[dict[str, Any]]) -> set[tuple[int, int]]:
        return {(group, COST[row["Rarity"]]) for row in echoes for group in row["FetterGroup"]}

    expected_pairs = pairs(list(items.values()))
    routes = []

    def add(
        identifier: str,
        name: str,
        location: str,
        kind: str,
        found: set[tuple[int, int]],
        evidence: str,
    ) -> None:
        if not found or found - expected_pairs:
            raise ValueError(f"Invalid combinations for {identifier}")
        routes.append(
            {
                "id": identifier,
                "name": name,
                "location": location,
                "kind": kind,
                "pairs": [[names[group], cost] for group, cost in sorted(found)],
                "evidence": evidence,
            }
        )

    nest_drops: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for enemy in tables["enemies"]:
        identifier = enemy["Id"]
        name = text.get(enemy["Name"])
        found = (
            [items[item] for item in ENCOUNTER_ECHO_IDS[identifier]]
            if identifier in ENCOUNTER_ECHO_IDS
            else by_name[enemy["Name"]]
        )
        if not found or not name:
            continue
        mark_id = enemy["MarkId"]
        available = pairs(found)
        if enemy["TypeDescription2"] == 23 and mark_id in range(7001, 7006):
            nest_drops[mark_id].update(available)
            continue
        if available and all(cost == 4 for _, cost in available):
            marker = markers.get(mark_id)
            if not marker:
                continue
            dungeon = weekly.get(entrances.get(mark_id, marker["RelativeId"]))
            location = LOCATIONS.get(identifier)
            kind = "週本" if dungeon or name == "鳴鐘之龜" else "首領"
            if dungeon:
                title = text[dungeon["Name"]].removesuffix("·戰歌重奏")
                location = " · ".join(
                    filter(
                        None, (location or regions[dungeon["DetectionTabType"]], "戰歌重奏", title)
                    )
                )
            elif not location:
                nav = "夢魘祓除" if enemy["NightMareDetectionId"] else "敵跡探尋"
                location = f"索拉指南 → {nav} → 追蹤 {name}"
            if identifier in ENCOUNTER_ECHO_IDS and len(found) == 1:
                name = text[found[0]["MonsterName"]]
            add(
                f"encounter:{identifier}",
                name,
                location,
                kind,
                available,
                BASE_URL + TABLES["dungeons" if dungeon else "enemies"],
            )

    for area in tables["activities"]:
        kind = area["Secondary"]
        if kind not in (63, 64) or area["Id"] not in range(30015001, 30015011):
            continue
        mark_id = area["MarkId"]
        marker = markers[mark_id]
        group_ids = set(area["PhantomFetterGroup"])
        if marker["MarkTitle"] != area["Name"] or area["BeginTimeStamp"] != 0:
            raise ValueError(f"Unverified nest: {mark_id}")
        if kind == 63:
            available = nest_drops[mark_id]
            if {group for group, _ in available} != group_ids:
                raise ValueError(f"Nightmare nest enemies do not match the guide: {mark_id}")
        else:
            available = {(group, cost) for group in group_ids for cost in (1, 3)} & expected_pairs
        add(
            f"nest:{mark_id}",
            text[area["Name"]],
            regions[area["DetectionTabType"]],
            "夢魘聚落" if kind == 63 else "殘象聚落",
            available,
            BASE_URL + TABLES["activities"],
        )

    return {
        "schema_version": 1,
        "game_version": "3.7.8",
        "source_revision": REVISION,
        "source_sha256": provenance,
        "catalog_pairs": [[names[group], cost] for group, cost in sorted(expected_pairs)],
        "routes": routes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = compile_catalog(args.source_directory)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
