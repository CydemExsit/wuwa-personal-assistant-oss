"""Turn stable, ordered observations into deterministic local echo events.

The tracker deliberately treats a session as temporal UI lineage, not as a
natural key made from the visible echo fields.  In particular, the enhancement
material page can show a new target and an old material echo at the same time;
those two UI roles must never be collapsed even when every visible field matches.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from wuwa_assistant.models.domain import (
    EchoEvent,
    EchoObservation,
    EchoSession,
    EchoStat,
    EventType,
    PageType,
    SessionState,
)
from wuwa_assistant.vision.selection_identity import visual_identity_matches

_TARGET_PAGES = {
    PageType.ECHO_SELECTION,
    PageType.ECHO_ENHANCEMENT,
    PageType.ENHANCEMENT_MATERIAL_SELECTION,
}


@dataclass(frozen=True, slots=True)
class _RevisitDecision:
    reusable_session: EchoSession | None = None
    possible_session: EchoSession | None = None
    has_visual_candidate: bool = False


class EchoStateTracker:
    """Track one ordered stream of already-stable observations.

    ``process_observation`` accepts stable semantic observations.  Frame
    debouncing belongs before this class; nevertheless, an identical consecutive
    semantic state is ignored so repeated stable captures are idempotent.
    """

    def __init__(self, *, defer_material_consumption: bool = False) -> None:
        self.sessions: dict[str, EchoSession] = {}
        self.events: list[EchoEvent] = []
        self.current_page: PageType | None = None
        self.current_session_id: str | None = None
        self.defer_material_consumption = defer_material_consumption

        self._next_session_number = 1
        self._last_observation: EchoObservation | None = None
        self._last_semantic_key: tuple[Any, ...] | None = None
        self._material_source_session_id: str | None = None
        self._material_target_session_id: str | None = None
        self._material_source_previous_state: SessionState | None = None

    @property
    def current_session(self) -> EchoSession | None:
        """Return the current target session, if one has been established."""

        if self.current_session_id is None:
            return None
        return self.sessions[self.current_session_id]

    def process(self, observation: EchoObservation) -> list[EchoEvent]:
        """Compatibility-friendly short alias for :meth:`process_observation`."""

        return self.process_observation(observation)

    def is_semantically_unchanged(self, observation: EchoObservation) -> bool:
        """Return whether ``observation`` is the exact state already accepted.

        An empty event list alone is not sufficient evidence of a duplicate: a
        changed observation can be appended to the current session without
        crossing an event boundary.  Live recognition uses this explicit query
        before preparing a tracker transaction so durable storage can skip only
        true semantic duplicates.
        """

        if observation.semantic_key() != self._last_semantic_key:
            return False
        previous = self._last_observation
        if observation.page_type is not PageType.ECHO_SELECTION or previous is None:
            return True
        left = previous.selection_identity
        right = observation.selection_identity
        if left is None and right is None:
            return True
        return visual_identity_matches(left, right)

    def process_many(self, observations: Iterable[EchoObservation]) -> list[EchoEvent]:
        """Process an ordered sequence and return only the newly emitted events."""

        emitted: list[EchoEvent] = []
        for observation in observations:
            emitted.extend(self.process_observation(observation))
        return emitted

    def process_observation(self, observation: EchoObservation) -> list[EchoEvent]:
        """Accept one stable observation and emit its state transitions."""

        semantic_key = observation.semantic_key()
        if self.is_semantically_unchanged(observation):
            return []

        emitted: list[EchoEvent] = []
        previous_page = self.current_page
        if previous_page != observation.page_type:
            emitted.append(self._page_changed_event(previous_page, observation))

        entering_material_page = (
            observation.page_type is PageType.ENHANCEMENT_MATERIAL_SELECTION
            and previous_page is not PageType.ENHANCEMENT_MATERIAL_SELECTION
        )

        if observation.page_type is PageType.ENHANCEMENT_MATERIAL_SELECTION:
            emitted.extend(self._process_material_observation(observation, entering_material_page))
        else:
            if self.defer_material_consumption and self._material_source_session_id is not None:
                emitted.extend(self._resolve_pending_material_observation(observation))
            if self._is_trackable_target(observation):
                emitted.extend(self._process_target_observation(observation))

        if (
            observation.page_type is not PageType.ENHANCEMENT_MATERIAL_SELECTION
            and self._material_source_session_id is None
        ):
            self._material_source_session_id = None
            self._material_target_session_id = None
            self._material_source_previous_state = None

        self.current_page = observation.page_type
        self._last_observation = observation
        self._last_semantic_key = semantic_key
        self.events.extend(emitted)
        return emitted

    def _process_target_observation(self, observation: EchoObservation) -> list[EchoEvent]:
        current = self.current_session
        if current is None or current.current_state is SessionState.CONSUMED_ENDED:
            return self._start_or_revisit(observation, current=current)

        previous = current.latest_observation
        if previous is not None and not self._observations_are_compatible(previous, observation):
            return self._start_or_revisit(observation, current=current)

        emitted = self._events_for_continuation(current, previous, observation)
        current.observations.append(observation)
        return emitted

    def _start_or_revisit(
        self,
        observation: EchoObservation,
        *,
        current: EchoSession | None,
    ) -> list[EchoEvent]:
        decision = self._find_revisit(observation, exclude=current)
        reusable = decision.reusable_session
        if reusable is not None:
            previous = reusable.latest_observation
            observation.revisit_status = "reused"
            observation.revisit_session_id = reusable.local_session_id
            emitted = self._events_for_continuation(reusable, previous, observation)
            reusable.observations.append(observation)
            self.current_session_id = reusable.local_session_id
            emitted.append(self._selected_event(reusable, observation, revisited=True))
            return emitted

        if decision.has_visual_candidate:
            observation.revisit_status = "possible"
            if decision.possible_session is not None:
                observation.revisit_session_id = decision.possible_session.local_session_id

        session, start_event = self._start_session(observation)
        emitted = [start_event]
        if observation.page_type is PageType.ECHO_SELECTION:
            emitted.append(self._selected_event(session, observation))
        return emitted

    def _find_revisit(
        self,
        observation: EchoObservation,
        *,
        exclude: EchoSession | None,
    ) -> _RevisitDecision:
        """Find only a uniquely proven revisit inside this tracker/capture run."""

        evidence = observation.selection_identity
        if observation.page_type is not PageType.ECHO_SELECTION or evidence is None:
            return _RevisitDecision()

        visual_candidates: list[tuple[EchoSession, EchoObservation]] = []
        for session in self.sessions.values():
            if (
                session is exclude
                or session.was_consumed
                or session.current_state
                in {SessionState.PENDING_CONSUMPTION, SessionState.CONSUMED_ENDED}
            ):
                continue
            reference = self._latest_selection_observation(session)
            if reference is not None and visual_identity_matches(
                reference.selection_identity,
                evidence,
            ):
                visual_candidates.append((session, reference))

        possible = visual_candidates[0][0] if len(visual_candidates) == 1 else None
        if len(visual_candidates) != 1:
            return _RevisitDecision(
                possible_session=possible,
                has_visual_candidate=bool(visual_candidates),
            )

        candidate, reference = visual_candidates[0]
        current_signature = self._complete_identity_signature(observation)
        reference_signature = self._complete_identity_signature(reference)
        if (
            current_signature is not None
            and reference_signature is not None
            and current_signature == reference_signature
        ):
            return _RevisitDecision(
                reusable_session=candidate,
                possible_session=candidate,
                has_visual_candidate=True,
            )
        return _RevisitDecision(possible_session=candidate, has_visual_candidate=True)

    @staticmethod
    def _latest_selection_observation(session: EchoSession) -> EchoObservation | None:
        for observation in reversed(session.observations):
            if (
                observation.page_type is PageType.ECHO_SELECTION
                and observation.selection_identity is not None
            ):
                return observation
        return None

    @staticmethod
    def _complete_identity_signature(observation: EchoObservation) -> tuple[Any, ...] | None:
        if (
            observation.page_type is not PageType.ECHO_SELECTION
            or observation.name is None
            or observation.level is None
            or observation.cost is None
            or len(observation.main_stats) != 2
        ):
            return None
        expected_substats = min(5, observation.level // 5)
        if len(observation.sub_stats) != expected_substats:
            return None
        stats = (*observation.main_stats, *observation.sub_stats)
        if any(stat.name is None or stat.value is None for stat in stats):
            return None
        return (
            observation.name,
            observation.level,
            observation.cost,
            tuple(stat.semantic_key for stat in observation.main_stats),
            tuple(stat.semantic_key for stat in observation.sub_stats),
        )

    def _process_material_observation(
        self,
        observation: EchoObservation,
        entering_material_page: bool,
    ) -> list[EchoEvent]:
        # Changes to material count, projected values, or confidence while the
        # same material page remains open belong to its already-created target.
        if not entering_material_page and self._material_target_session_id is not None:
            target = self.sessions[self._material_target_session_id]
            previous = target.latest_observation
            emitted = self._events_for_continuation(target, previous, observation)
            target.observations.append(observation)
            self.current_session_id = target.local_session_id
            return emitted

        source = self.current_session
        source_is_known = (
            source is not None
            and source.current_state is not SessionState.CONSUMED_ENDED
            and observation.material_source_selected is True
        )

        target, start_event = self._start_session(observation, force_successor=source_is_known)
        self._material_target_session_id = target.local_session_id
        emitted = [start_event]

        if source_is_known and source is not None:
            self._material_source_session_id = source.local_session_id
            self._material_source_previous_state = source.current_state
            source.current_state = SessionState.PENDING_CONSUMPTION
            if not self.defer_material_consumption:
                emitted.extend(self._commit_material_consumption(source, target, observation))

        return emitted

    def cancel_pending_consumption(self) -> None:
        """Restore a source echo when capture stops before consumption is confirmed.

        Merely selecting a material is reversible in the game UI.  Live mode calls
        this method on stop/error so an unconfirmed selection can never be persisted
        as a consumed echo.  The offline PoC keeps its original immediate behavior
        unless ``defer_material_consumption`` is enabled explicitly.
        """

        if self._material_source_session_id is not None:
            source = self.sessions[self._material_source_session_id]
            if source.current_state is SessionState.PENDING_CONSUMPTION:
                source.current_state = self._material_source_previous_state or SessionState.ACTIVE
        self._material_source_session_id = None
        self._material_target_session_id = None
        self._material_source_previous_state = None

    def _resolve_pending_material_observation(
        self,
        observation: EchoObservation,
    ) -> list[EchoEvent]:
        source_id = self._material_source_session_id
        target_id = self._material_target_session_id
        if source_id is None or target_id is None:
            self.cancel_pending_consumption()
            return []

        source = self.sessions[source_id]
        target = self.sessions[target_id]
        confirmed = self._is_material_consumption_confirmation(target, observation)
        if confirmed:
            emitted = self._commit_material_consumption(source, target, observation)
            self._material_source_session_id = None
            self._material_target_session_id = None
            self._material_source_previous_state = None
            return emitted

        self.cancel_pending_consumption()
        return []

    def _commit_material_consumption(
        self,
        source: EchoSession,
        target: EchoSession,
        observation: EchoObservation,
    ) -> list[EchoEvent]:
        source.was_consumed = True
        source.consumed_into_session_id = target.local_session_id
        source.current_state = SessionState.CONSUMED_ENDED
        return [
            self._used_as_material_event(source, target, observation),
            self._ended_event(source, target, observation),
        ]

    @staticmethod
    def _is_material_consumption_confirmation(
        target: EchoSession,
        observation: EchoObservation,
    ) -> bool:
        """Require a subsequent stable enhanced target before committing consumption."""

        previous = target.latest_observation
        if (
            previous is None
            or previous.page_type is not PageType.ENHANCEMENT_MATERIAL_SELECTION
            or observation.page_type is not PageType.ECHO_ENHANCEMENT
            or not EchoStateTracker._observations_are_compatible(previous, observation)
            or observation.level is None
        ):
            return False
        if previous.projected_level is not None:
            return observation.level >= previous.projected_level
        if previous.level is not None:
            return observation.level > previous.level
        return bool(observation.sub_stats)

    def _events_for_continuation(
        self,
        session: EchoSession,
        previous: EchoObservation | None,
        current: EchoObservation,
    ) -> list[EchoEvent]:
        emitted: list[EchoEvent] = []

        new_substats = self._new_substats(previous, current)
        level_increased = (
            previous is not None
            and previous.level is not None
            and current.level is not None
            and current.level > previous.level
        )
        if previous is not None and (level_increased or new_substats):
            emitted.append(
                EchoEvent(
                    event_type=EventType.ECHO_UPGRADED,
                    session_id=session.local_session_id,
                    previous_observation=previous,
                    current_observation=current,
                    confidence=self._transition_confidence(previous, current),
                    previous_level=previous.level,
                    current_level=current.level,
                    new_substats=new_substats,
                    source_file=current.source_file,
                )
            )

        if current.is_discarded is True and not session.is_discarded:
            session.is_discarded = True
            session.current_state = SessionState.DISCARDED_ACTIVE
            emitted.append(
                EchoEvent(
                    event_type=EventType.ECHO_MARKED_DISCARDED,
                    session_id=session.local_session_id,
                    previous_observation=previous,
                    current_observation=current,
                    confidence=current.overall_confidence,
                    notes=["discarded flag changed to true"],
                    source_file=current.source_file,
                )
            )
        elif (
            current.is_discarded is False and session.current_state is SessionState.DISCARDED_ACTIVE
        ):
            # The persistent toggle can also confirm that a mark was removed.
            # Keep historical events, and leave unknown or material states alone.
            session.is_discarded = False
            session.current_state = SessionState.ACTIVE

        return emitted

    def _start_session(
        self,
        observation: EchoObservation,
        *,
        force_successor: bool = False,
    ) -> tuple[EchoSession, EchoEvent]:
        had_sessions = bool(self.sessions)
        session_id = f"echo-session-{self._next_session_number:04d}"
        self._next_session_number += 1

        session = EchoSession(local_session_id=session_id, observations=[observation])
        if observation.is_discarded is True:
            session.is_discarded = True
            session.current_state = SessionState.DISCARDED_ACTIVE
        self.sessions[session_id] = session
        self.current_session_id = session_id

        event_type = (
            EventType.NEW_ECHO_SESSION_STARTED
            if had_sessions or force_successor
            else EventType.ECHO_SESSION_STARTED
        )
        notes = (
            ["material screen target"]
            if observation.page_type is PageType.ENHANCEMENT_MATERIAL_SELECTION
            else []
        )
        if observation.revisit_status == "possible":
            notes.append("possible_revisit=true")
        event = EchoEvent(
            event_type=event_type,
            session_id=session_id,
            current_observation=observation,
            confidence=observation.overall_confidence,
            notes=notes,
            source_file=observation.source_file,
        )
        return session, event

    def _page_changed_event(
        self,
        previous_page: PageType | None,
        current: EchoObservation,
    ) -> EchoEvent:
        previous_text = "null" if previous_page is None else previous_page.value
        page_confidence = current.confidence.get("page_type", current.overall_confidence)
        return EchoEvent(
            event_type=EventType.PAGE_CHANGED,
            session_id=None,
            previous_observation=self._last_observation,
            current_observation=current,
            confidence=page_confidence,
            notes=[
                f"previous_page_type={previous_text}",
                f"current_page_type={current.page_type.value}",
            ],
            source_file=current.source_file,
        )

    @staticmethod
    def _selected_event(
        session: EchoSession,
        observation: EchoObservation,
        *,
        revisited: bool = False,
    ) -> EchoEvent:
        return EchoEvent(
            event_type=EventType.ECHO_SELECTED,
            session_id=session.local_session_id,
            current_observation=observation,
            confidence=observation.overall_confidence,
            notes=["revisited=true"] if revisited else [],
            source_file=observation.source_file,
        )

    @staticmethod
    def _used_as_material_event(
        source: EchoSession,
        target: EchoSession,
        observation: EchoObservation,
    ) -> EchoEvent:
        previous = source.latest_observation
        return EchoEvent(
            event_type=EventType.ECHO_USED_AS_MATERIAL,
            session_id=source.local_session_id,
            related_session_id=target.local_session_id,
            previous_observation=previous,
            current_observation=observation,
            confidence=EchoStateTracker._transition_confidence(previous, observation),
            notes=[f"consumed_into={target.local_session_id}"],
            source_file=observation.source_file,
        )

    @staticmethod
    def _ended_event(
        source: EchoSession,
        target: EchoSession,
        observation: EchoObservation,
    ) -> EchoEvent:
        previous = source.latest_observation
        return EchoEvent(
            event_type=EventType.ECHO_SESSION_ENDED,
            session_id=source.local_session_id,
            related_session_id=target.local_session_id,
            previous_observation=previous,
            current_observation=observation,
            confidence=EchoStateTracker._transition_confidence(previous, observation),
            notes=["reason=consumed"],
            source_file=observation.source_file,
        )

    @staticmethod
    def _is_trackable_target(observation: EchoObservation) -> bool:
        return observation.page_type in _TARGET_PAGES and any(
            (
                observation.name is not None,
                observation.level is not None,
                observation.cost is not None,
                bool(observation.main_stats),
                bool(observation.sub_stats),
            )
        )

    @staticmethod
    def _observations_are_compatible(
        previous: EchoObservation,
        current: EchoObservation,
    ) -> bool:
        if (
            previous.page_type is PageType.ECHO_SELECTION
            and current.page_type is PageType.ECHO_SELECTION
            and previous.selection_identity is not None
            and current.selection_identity is not None
            and not visual_identity_matches(
                previous.selection_identity,
                current.selection_identity,
            )
        ):
            return False
        # Missing OCR fields carry no negative evidence.
        if previous.name is not None and current.name is not None and previous.name != current.name:
            return False
        if previous.cost is not None and current.cost is not None and previous.cost != current.cost:
            return False
        if (
            previous.level is not None
            and current.level is not None
            and current.level < previous.level
        ):
            return False

        previous_main = [(stat.name, stat.unit) for stat in previous.main_stats if stat.name]
        current_main = [(stat.name, stat.unit) for stat in current.main_stats if stat.name]
        if previous_main and current_main and previous_main != current_main:
            return False

        # At the same level, a wholly different non-empty substat set is strong
        # evidence of another echo.  Partial OCR omissions remain compatible.
        if (
            previous.level is not None
            and current.level == previous.level
            and previous.sub_stats
            and current.sub_stats
        ):
            previous_keys = {EchoStateTracker._stat_key(stat) for stat in previous.sub_stats}
            current_keys = {EchoStateTracker._stat_key(stat) for stat in current.sub_stats}
            if previous_keys.isdisjoint(current_keys):
                return False

        return True

    @staticmethod
    def _new_substats(
        previous: EchoObservation | None,
        current: EchoObservation,
    ) -> list[EchoStat]:
        if previous is None:
            return []
        previous_keys = {EchoStateTracker._stat_key(stat) for stat in previous.sub_stats}
        return [
            stat
            for stat in current.sub_stats
            if EchoStateTracker._stat_key(stat) not in previous_keys
        ]

    @staticmethod
    def _stat_key(stat: EchoStat) -> tuple[int | None, str | None, int | float | None, str | None]:
        return (stat.slot_index, stat.name, stat.value, stat.unit)

    @staticmethod
    def _transition_confidence(
        previous: EchoObservation | None,
        current: EchoObservation,
    ) -> float | None:
        values = [current.overall_confidence]
        if previous is not None:
            values.append(previous.overall_confidence)
        confirmed = [value for value in values if value is not None]
        return min(confirmed) if confirmed else None
