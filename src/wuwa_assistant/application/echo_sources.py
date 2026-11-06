"""Offline acquisition routes from the pinned Global release catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from wuwa_assistant.planning.farming import FarmZone


@dataclass(frozen=True, slots=True)
class EchoSourceCatalog:
    game_version: str
    pairs: frozenset[tuple[str, int]]
    routes: tuple[FarmZone, ...]


@lru_cache(maxsize=1)
def load_echo_sources() -> EchoSourceCatalog:
    raw = files("wuwa_assistant.data.planning").joinpath("echo-sources.v1.json")
    payload = json.loads(raw.read_text(encoding="utf-8"))
    if payload["schema_version"] != 1:
        raise ValueError("Unsupported Echo acquisition catalog schema")
    routes = []
    for row in payload["routes"]:
        pairs = tuple((name, cost) for name, cost in row["pairs"])
        routes.append(
            FarmZone(
                row["id"],
                row["name"],
                row["location"],
                tuple(dict.fromkeys(name for name, _ in pairs)),
                row["evidence"],
                tuple(sorted({cost for _, cost in pairs})),
                row["kind"],
                pairs,
            )
        )
    return EchoSourceCatalog(
        payload["game_version"],
        frozenset((name, cost) for name, cost in payload["catalog_pairs"]),
        tuple(routes),
    )
