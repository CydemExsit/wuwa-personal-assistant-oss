from __future__ import annotations

from wuwa_assistant.models.domain import (
    EchoObservation,
    EchoStat,
    EventType,
    PageType,
    SelectionIdentityEvidence,
    SessionState,
    StatKind,
)
from wuwa_assistant.state import EchoStateTracker


def stat(
    kind: StatKind,
    name: str,
    value: int | float,
    unit: str | None,
    slot: int | None = None,
) -> EchoStat:
    return EchoStat(
        stat_kind=kind,
        name=name,
        value=value,
        unit=unit,
        confidence=0.99,
        name_confidence=0.99,
        value_confidence=0.99,
        slot_index=slot,
    )


def hp_stats(percent: float, flat: int) -> list[EchoStat]:
    return [
        stat(StatKind.PRIMARY, "生命", percent, "%"),
        stat(StatKind.FIXED, "生命", flat, None),
    ]


def identity(cell: int, pattern: int) -> SelectionIdentityEvidence:
    value = f"{pattern:016x}"
    return SelectionIdentityEvidence(
        version="selection-grid-v1",
        selected_cell=cell,
        selected_card_bbox=(5 + 179 * ((cell - 1) % 3), 8, 152, 190),
        card_art_phash=value,
        context_phashes=tuple(value for _index in range(15)),
    )


def observation(
    page: PageType,
    index: int,
    *,
    level: int | None = None,
    projected_level: int | None = None,
    cost: int | None = None,
    main_stats: list[EchoStat] | None = None,
    sub_stats: list[EchoStat] | None = None,
    locked: int | None = None,
    discarded: bool | None = None,
    material_selected: bool | None = None,
    material_level: int | None = None,
    selection_identity: SelectionIdentityEvidence | None = None,
) -> EchoObservation:
    return EchoObservation(
        page_type=page,
        name="不熄獵手" if page is not PageType.CHARACTER_ECHO_OVERVIEW else None,
        level=level,
        projected_level=projected_level,
        cost=cost,
        main_stats=main_stats or [],
        sub_stats=sub_stats or [],
        locked_substat_slots=locked,
        confidence={"page_type": 0.99, "name": 0.98, "level": 0.99},
        source_file=f"{index}.png",
        is_discarded=discarded,
        material_source_selected=material_selected,
        material_source_level=material_level,
        selection_identity=selection_identity,
    )


def nine_observations() -> list[EchoObservation]:
    crit = stat(StatKind.SUBSTAT, "暴擊", 7.5, "%", 0)
    liberation = stat(StatKind.SUBSTAT, "共鳴解放傷害加成", 9.4, "%", 1)
    attack = stat(StatKind.SUBSTAT, "攻擊", 10.1, "%", 2)
    skill = stat(StatKind.SUBSTAT, "共鳴技能傷害加成", 8.6, "%", 0)
    normal = stat(StatKind.SUBSTAT, "普攻傷害加成", 7.1, "%", 1)
    heavy = stat(StatKind.SUBSTAT, "重擊傷害加成", 8.6, "%", 2)

    return [
        observation(PageType.CHARACTER_ECHO_OVERVIEW, 1),
        observation(
            PageType.ECHO_SELECTION,
            2,
            level=0,
            cost=1,
            main_stats=hp_stats(4.5, 456),
        ),
        observation(
            PageType.ECHO_ENHANCEMENT,
            3,
            level=0,
            main_stats=hp_stats(4.5, 456),
            locked=5,
        ),
        observation(
            PageType.ECHO_ENHANCEMENT,
            4,
            level=5,
            main_stats=hp_stats(8.2, 820),
            sub_stats=[crit],
            locked=4,
        ),
        observation(
            PageType.ECHO_ENHANCEMENT,
            5,
            level=10,
            main_stats=hp_stats(11.8, 1185),
            sub_stats=[crit, liberation],
            locked=3,
        ),
        observation(
            PageType.ECHO_ENHANCEMENT,
            6,
            level=15,
            main_stats=hp_stats(15.5, 1550),
            sub_stats=[crit, liberation, attack],
            locked=2,
        ),
        observation(
            PageType.ECHO_SELECTION,
            7,
            level=15,
            cost=1,
            main_stats=hp_stats(15.5, 1550),
            sub_stats=[crit, liberation, attack],
            locked=2,
            discarded=True,
        ),
        observation(
            PageType.ENHANCEMENT_MATERIAL_SELECTION,
            8,
            level=0,
            projected_level=15,
            cost=1,
            main_stats=hp_stats(4.5, 456),
            locked=5,
            material_selected=True,
            material_level=15,
        ),
        observation(
            PageType.ECHO_ENHANCEMENT,
            9,
            level=15,
            main_stats=hp_stats(15.5, 1550),
            sub_stats=[skill, normal, heavy],
            locked=2,
        ),
    ]


def event_signature(tracker: EchoStateTracker) -> list[tuple[EventType, str | None]]:
    return [(event.event_type, event.session_id) for event in tracker.events]


def test_nine_image_event_timeline_and_sessions() -> None:
    tracker = EchoStateTracker()
    tracker.process_many(nine_observations())

    assert event_signature(tracker) == [
        (EventType.PAGE_CHANGED, None),
        (EventType.PAGE_CHANGED, None),
        (EventType.ECHO_SESSION_STARTED, "echo-session-0001"),
        (EventType.ECHO_SELECTED, "echo-session-0001"),
        (EventType.PAGE_CHANGED, None),
        (EventType.ECHO_UPGRADED, "echo-session-0001"),
        (EventType.ECHO_UPGRADED, "echo-session-0001"),
        (EventType.ECHO_UPGRADED, "echo-session-0001"),
        (EventType.PAGE_CHANGED, None),
        (EventType.ECHO_MARKED_DISCARDED, "echo-session-0001"),
        (EventType.PAGE_CHANGED, None),
        (EventType.NEW_ECHO_SESSION_STARTED, "echo-session-0002"),
        (EventType.ECHO_USED_AS_MATERIAL, "echo-session-0001"),
        (EventType.ECHO_SESSION_ENDED, "echo-session-0001"),
        (EventType.PAGE_CHANGED, None),
        (EventType.ECHO_UPGRADED, "echo-session-0002"),
    ]

    first = tracker.sessions["echo-session-0001"]
    second = tracker.sessions["echo-session-0002"]
    assert first.current_state is SessionState.CONSUMED_ENDED
    assert first.is_discarded is True
    assert first.was_consumed is True
    assert first.consumed_into_session_id == second.local_session_id
    assert second.current_state is SessionState.ACTIVE
    assert second.latest_observation is not None
    assert second.latest_observation.level == 15

    first_upgrades = [
        event
        for event in tracker.events
        if event.event_type is EventType.ECHO_UPGRADED and event.session_id == "echo-session-0001"
    ]
    assert [(event.previous_level, event.current_level) for event in first_upgrades] == [
        (0, 5),
        (5, 10),
        (10, 15),
    ]
    assert [[item.name for item in event.new_substats] for event in first_upgrades] == [
        ["暴擊"],
        ["共鳴解放傷害加成"],
        ["攻擊"],
    ]

    second_upgrade = [
        event
        for event in tracker.events
        if event.event_type is EventType.ECHO_UPGRADED and event.session_id == "echo-session-0002"
    ]
    assert len(second_upgrade) == 1
    assert (second_upgrade[0].previous_level, second_upgrade[0].current_level) == (0, 15)
    assert [item.name for item in second_upgrade[0].new_substats] == [
        "共鳴技能傷害加成",
        "普攻傷害加成",
        "重擊傷害加成",
    ]


def test_repeated_stable_observations_emit_no_duplicate_events() -> None:
    tracker = EchoStateTracker()
    observations = nine_observations()

    for item in observations:
        first_result = tracker.process_observation(item)
        second_result = tracker.process_observation(item)
        assert first_result
        assert second_result == []

    assert len(tracker.sessions) == 2
    assert len(tracker.events) == 16


def test_discard_toggle_can_be_cleared_and_marked_again() -> None:
    tracker = EchoStateTracker()
    tracker.process_observation(
        observation(PageType.ECHO_SELECTION, 1, level=0, cost=1, discarded=False)
    )
    session = tracker.current_session
    assert session is not None

    marked = observation(PageType.ECHO_SELECTION, 2, level=0, cost=1, discarded=True)
    assert [event.event_type for event in tracker.process_observation(marked)] == [
        EventType.ECHO_MARKED_DISCARDED
    ]
    unknown = observation(PageType.ECHO_SELECTION, 3, level=0, cost=1, discarded=None)
    assert tracker.process_observation(unknown) == []
    assert session.is_discarded is True
    assert session.current_state is SessionState.DISCARDED_ACTIVE

    cleared = observation(PageType.ECHO_SELECTION, 4, level=0, cost=1, discarded=False)
    assert tracker.is_semantically_unchanged(cleared) is False
    assert tracker.process_observation(cleared) == []
    assert tracker.current_session is session
    assert session.latest_observation is cleared
    assert session.is_discarded is False
    assert session.current_state is SessionState.ACTIVE
    assert tracker.is_semantically_unchanged(cleared) is True

    remarked = observation(PageType.ECHO_SELECTION, 5, level=0, cost=1, discarded=True)
    assert [event.event_type for event in tracker.process_observation(remarked)] == [
        EventType.ECHO_MARKED_DISCARDED
    ]
    assert session.is_discarded is True
    assert session.current_state is SessionState.DISCARDED_ACTIVE
    assert len(tracker.sessions) == 1
    assert sum(event.event_type is EventType.ECHO_MARKED_DISCARDED for event in tracker.events) == 2


def test_clear_discard_observation_does_not_reopen_consumed_source() -> None:
    tracker = EchoStateTracker()
    tracker.process_many(nine_observations()[:8])
    source = tracker.sessions["echo-session-0001"]
    assert source.current_state is SessionState.CONSUMED_ENDED

    tracker.process_observation(
        observation(PageType.ECHO_SELECTION, 9, level=0, cost=1, discarded=False)
    )

    assert tracker.current_session is not source
    assert source.current_state is SessionState.CONSUMED_ENDED
    assert source.is_discarded is True
    assert source.was_consumed is True


def test_changed_material_screen_does_not_create_or_consume_again() -> None:
    tracker = EchoStateTracker()
    observations = nine_observations()
    tracker.process_many(observations[:8])

    changed_material = observation(
        PageType.ENHANCEMENT_MATERIAL_SELECTION,
        80,
        level=0,
        projected_level=15,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        locked=5,
        material_selected=True,
        material_level=15,
    )
    changed_material.projected_main_stats = hp_stats(15.5, 1550)

    assert tracker.process_observation(changed_material) == []
    assert list(tracker.sessions) == ["echo-session-0001", "echo-session-0002"]
    assert sum(event.event_type is EventType.ECHO_USED_AS_MATERIAL for event in tracker.events) == 1


def test_same_looking_target_after_material_use_never_reuses_source_id() -> None:
    tracker = EchoStateTracker()
    observations = nine_observations()
    tracker.process_many(observations[:8])

    source = tracker.sessions["echo-session-0001"]
    target = tracker.sessions["echo-session-0002"]
    assert source.latest_observation is not None
    assert target.latest_observation is not None
    assert source.latest_observation.name == target.latest_observation.name
    assert source.consumed_into_session_id == target.local_session_id
    assert tracker.current_session_id == target.local_session_id


def test_wholly_replaced_substats_do_not_mutate_existing_session() -> None:
    tracker = EchoStateTracker()
    observations = nine_observations()
    tracker.process_many(observations[:6])

    emitted = tracker.process_observation(observations[8])

    assert [event.event_type for event in emitted] == [
        EventType.NEW_ECHO_SESSION_STARTED,
    ]
    assert tracker.current_session_id == "echo-session-0002"
    assert tracker.sessions["echo-session-0001"].was_consumed is False
    assert tracker.sessions["echo-session-0001"].current_state is SessionState.ACTIVE


def test_same_level_new_substat_is_an_upgrade_once() -> None:
    tracker = EchoStateTracker()
    base = observation(
        PageType.ECHO_ENHANCEMENT,
        1,
        level=5,
        main_stats=hp_stats(8.2, 820),
        locked=4,
    )
    tuned = observation(
        PageType.ECHO_ENHANCEMENT,
        2,
        level=5,
        main_stats=hp_stats(8.2, 820),
        sub_stats=[stat(StatKind.SUBSTAT, "暴擊", 7.5, "%", 0)],
        locked=4,
    )

    tracker.process_observation(base)
    emitted = tracker.process_observation(tuned)

    assert [event.event_type for event in emitted] == [EventType.ECHO_UPGRADED]
    assert emitted[0].previous_level == emitted[0].current_level == 5
    assert tracker.process_observation(tuned) == []


def test_live_mode_defers_material_consumption_until_enhanced_target_is_seen() -> None:
    tracker = EchoStateTracker(defer_material_consumption=True)
    observations = nine_observations()
    tracker.process_many(observations[:8])

    source = tracker.sessions["echo-session-0001"]
    target = tracker.sessions["echo-session-0002"]
    assert source.current_state is SessionState.PENDING_CONSUMPTION
    assert source.was_consumed is False
    assert not any(event.event_type is EventType.ECHO_USED_AS_MATERIAL for event in tracker.events)

    emitted = tracker.process_observation(observations[8])

    assert [event.event_type for event in emitted] == [
        EventType.PAGE_CHANGED,
        EventType.ECHO_USED_AS_MATERIAL,
        EventType.ECHO_SESSION_ENDED,
        EventType.ECHO_UPGRADED,
    ]
    assert source.current_state is SessionState.CONSUMED_ENDED
    assert source.was_consumed is True
    assert source.consumed_into_session_id == target.local_session_id


def test_live_mode_stop_restores_unconfirmed_material_source() -> None:
    tracker = EchoStateTracker(defer_material_consumption=True)
    observations = nine_observations()
    tracker.process_many(observations[:8])

    source = tracker.sessions["echo-session-0001"]
    assert source.current_state is SessionState.PENDING_CONSUMPTION

    tracker.cancel_pending_consumption()

    assert source.current_state is SessionState.DISCARDED_ACTIVE
    assert source.was_consumed is False
    assert source.consumed_into_session_id is None


def test_strict_visual_revisit_returns_to_prior_session_without_merging_distinct_card() -> None:
    tracker = EchoStateTracker()
    first = observation(
        PageType.ECHO_SELECTION,
        1,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    second = observation(
        PageType.ECHO_SELECTION,
        2,
        level=0,
        cost=1,
        main_stats=hp_stats(6.0, 456),
        selection_identity=identity(2, 0xEEEE),
    )
    revisit = observation(
        PageType.ECHO_SELECTION,
        3,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )

    tracker.process_many((first, second))
    emitted = tracker.process_observation(revisit)

    assert len(tracker.sessions) == 2
    assert tracker.current_session_id == "echo-session-0001"
    assert len(tracker.sessions["echo-session-0001"].observations) == 2
    assert revisit.revisit_status == "reused"
    assert revisit.revisit_session_id == "echo-session-0001"
    assert [event.event_type for event in emitted] == [EventType.ECHO_SELECTED]
    assert emitted[0].notes == ["revisited=true"]


def test_identical_visible_stats_on_another_cell_start_a_new_session() -> None:
    tracker = EchoStateTracker()
    first = observation(
        PageType.ECHO_SELECTION,
        1,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    distinct = observation(
        PageType.ECHO_SELECTION,
        2,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(2, 0xEEEE),
    )

    tracker.process_observation(first)
    emitted = tracker.process_observation(distinct)

    assert len(tracker.sessions) == 2
    assert tracker.current_session_id == "echo-session-0002"
    assert distinct.revisit_status is None
    assert [event.event_type for event in emitted] == [
        EventType.NEW_ECHO_SESSION_STARTED,
        EventType.ECHO_SELECTED,
    ]


def test_visual_match_with_conflicting_signature_is_only_a_possible_revisit() -> None:
    tracker = EchoStateTracker()
    first = observation(
        PageType.ECHO_SELECTION,
        1,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    middle = observation(
        PageType.ECHO_SELECTION,
        2,
        level=0,
        cost=1,
        main_stats=hp_stats(6.0, 456),
        selection_identity=identity(2, 0xEEEE),
    )
    possible = observation(
        PageType.ECHO_SELECTION,
        3,
        level=0,
        cost=1,
        main_stats=hp_stats(7.5, 456),
        selection_identity=identity(1, 0x1111),
    )

    tracker.process_many((first, middle))
    emitted = tracker.process_observation(possible)

    assert len(tracker.sessions) == 3
    assert possible.revisit_status == "possible"
    assert possible.revisit_session_id == "echo-session-0001"
    assert emitted[0].notes == ["possible_revisit=true"]


def test_consumed_session_is_never_revived_by_matching_visual_evidence() -> None:
    tracker = EchoStateTracker()
    first = observation(
        PageType.ECHO_SELECTION,
        1,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    middle = observation(
        PageType.ECHO_SELECTION,
        2,
        level=0,
        cost=1,
        main_stats=hp_stats(6.0, 456),
        selection_identity=identity(2, 0xEEEE),
    )
    tracker.process_many((first, middle))
    consumed = tracker.sessions["echo-session-0001"]
    consumed.was_consumed = True
    consumed.current_state = SessionState.CONSUMED_ENDED

    revisit = observation(
        PageType.ECHO_SELECTION,
        3,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    tracker.process_observation(revisit)

    assert len(tracker.sessions) == 3
    assert tracker.current_session_id == "echo-session-0003"
    assert revisit.revisit_status is None
    assert consumed.current_state is SessionState.CONSUMED_ENDED


def test_new_tracker_capture_never_reuses_prior_capture_identity() -> None:
    prior_capture = EchoStateTracker()
    seen = observation(
        PageType.ECHO_SELECTION,
        1,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    prior_capture.process_observation(seen)

    next_capture = EchoStateTracker()
    same_visual = observation(
        PageType.ECHO_SELECTION,
        2,
        level=0,
        cost=1,
        main_stats=hp_stats(4.5, 456),
        selection_identity=identity(1, 0x1111),
    )
    next_capture.process_observation(same_visual)

    assert same_visual.revisit_status is None
    assert len(next_capture.sessions) == 1
    assert next_capture.current_session_id == "echo-session-0001"
    assert (
        next_capture.sessions["echo-session-0001"]
        is not prior_capture.sessions["echo-session-0001"]
    )
