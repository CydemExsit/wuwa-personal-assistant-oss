from __future__ import annotations

import pytest

from wuwa_assistant.application.echo_sources import load_echo_sources
from wuwa_assistant.application.tacet_catalog import load_tacet_catalog
from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmRole,
    FarmZone,
    recommend_zones,
)


def _character(echo, sets=()):
    return FarmCharacter("c", "測試角色", FarmRole.MAIN, "主輸出", sets, (echo,))


def test_pinned_release_every_existing_sonata_cost_has_a_trackable_route():
    catalog = load_echo_sources()
    tacet = load_tacet_catalog()
    assert catalog.game_version == tacet.game_version == "3.7.8"
    assert len(catalog.pairs) == 109
    assert len({name for name, _ in catalog.pairs}) == 37
    assert len(tacet.fields) == 21
    assert len({zone.identifier for zone in catalog.routes}) == len(catalog.routes)
    assert {zone.kind for zone in catalog.routes} == {
        "首領",
        "週本",
        "夢魘聚落",
        "殘象聚落",
    }
    zones = (
        *catalog.routes,
        *(FarmZone(f.field_id, f.name, f.region, f.set_names) for f in tacet.fields),
    )
    for set_name, cost in catalog.pairs:
        echo = FarmEcho(2, cost, 25, 50.0, set_name, None, None)
        plan = recommend_zones(
            (_character(echo),),
            zones,
            requested_slots={("c", 2): cost},
        )
        assert plan.recommendations, (set_name, cost)
        assert all(
            row.zone.supports(set_name, cost) and row.zone.region for row in plan.recommendations
        )


@pytest.mark.parametrize(
    ("set_name", "name", "location", "kind"),
    [
        ("願戴榮光之旅", "夢魘·凱爾匹", "七丘 · 哀慟谷", "首領"),
        ("銜夢照世之心", "共鳴迴響·天演溯心", "夢玄溪山 · 戰歌重奏 · 定序諸理之律", "週本"),
    ],
)
def test_preexisting_cost_four_mark_shows_requested_echo_in_plan(set_name, name, location, kind):
    catalog = load_echo_sources()
    echo = FarmEcho(2, 4, 25, 50.0, set_name, None, True)
    plan = recommend_zones(
        (_character(echo),),
        (FarmZone("tacet", "無音區", "測試地區", (set_name,)), *catalog.routes),
        requested_slots={("c", 2): 4},
    )
    route = next(row for row in plan.recommendations if row.zone.name == name)
    assert location in route.zone.region and route.zone.kind == kind
    assert route.benefits[0].slot_index == 2
    assert all(row.zone.kind != "無音區" for row in plan.recommendations)
    assert all("確切地點待查" not in notice for notice in plan.notices)


def test_cost_and_sonata_must_be_verified_as_one_pair_and_explicit_target_wins():
    route = FarmZone(
        "source", "活動", "測試", ("A", "B"), costs=(1, 4), set_costs=(("A", 1), ("B", 4))
    )
    echo = FarmEcho(2, 4, 25, 50.0, "B", None, None, target_sets=("A",))
    plan = recommend_zones((_character(echo),), (route,), requested_slots={("c", 2): 4})
    assert not plan.recommendations
    assert any("COST 4" in notice and "確切地點待查" in notice for notice in plan.notices)
