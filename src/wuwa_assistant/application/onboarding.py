"""Persistent first-run state and safely isolated demonstration data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    CreateTeam,
    TeamMemberInput,
)
from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.display import (
    DEFAULT_RESOLUTION,
    DISPLAY_MODE_KEY,
    DISPLAY_SETUP_KEY,
    RESOLUTION_KEY,
    DisplayPreferences,
)
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import DataSetRecord
from wuwa_assistant.storage.settings import SettingsService

ONBOARDING_STATE_KEY = "onboarding/state"
CAPTURE_MONITOR_SETTING_KEY = "capture/monitor_id"
SCORING_TEMPLATE_SETTING_KEY = "scoring/onboarding_template_kind"
ONBOARDING_SCHEMA_VERSION = 1
DEMO_DATA_SET_NAME = "示範資料 (虛構範例)"


class OnboardingMode(StrEnum):
    QUICK_START = "quick_start"
    FIRST_CHARACTER = "first_character"
    DEMO_DATA = "demo_data"


@dataclass(frozen=True, slots=True)
class OnboardingCommand:
    monitor_identifier: str
    data_path: Path
    mode: OnboardingMode
    safety_confirmed: bool
    capture_test_confirmed: bool
    data_path_confirmed: bool
    character_name: str | None = None
    scoring_template_kind: str = "generic_dps"
    display_mode: str = "dual"
    resolution: str = DEFAULT_RESOLUTION

    def __post_init__(self) -> None:
        DisplayPreferences(mode=self.display_mode, resolution=self.resolution)
        if not isinstance(self.monitor_identifier, str) or not self.monitor_identifier.strip():
            raise ValueError("monitor_identifier must not be blank")
        if len(self.monitor_identifier.strip()) > 200:
            raise ValueError("monitor_identifier must not exceed 200 characters")
        if not isinstance(self.data_path, Path) or not self.data_path.is_absolute():
            raise ValueError("data_path must be an absolute Path")
        if not isinstance(self.mode, OnboardingMode):
            raise ValueError("mode must be an OnboardingMode")
        if not self.safety_confirmed:
            raise ValueError("the safety boundary must be confirmed")
        if not self.capture_test_confirmed:
            raise ValueError("the passive capture test must be confirmed")
        if not self.data_path_confirmed:
            raise ValueError("the local data path must be confirmed")
        name = None if self.character_name is None else self.character_name.strip()
        if self.mode is OnboardingMode.FIRST_CHARACTER and not name:
            raise ValueError("character_name is required for first_character mode")
        if name is not None and len(name) > 120:
            raise ValueError("character_name must not exceed 120 characters")
        if self.scoring_template_kind not in _SCORING_TEMPLATE_KINDS:
            raise ValueError("scoring_template_kind is not supported")


@dataclass(frozen=True, slots=True)
class OnboardingState:
    completed: bool = False
    schema_version: int = ONBOARDING_SCHEMA_VERSION
    completed_at: datetime | None = None
    monitor_identifier: str | None = None
    data_path: Path | None = None
    mode: OnboardingMode | None = None
    data_set_id: str | None = None
    character_id: str | None = None
    scoring_template_kind: str = "generic_dps"
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DemoDataSnapshot:
    data_set_id: str
    character_count: int
    team_count: int
    created_new_dataset: bool


class DemoDataService:
    """Create and remove a public, fictional dataset without touching primary data."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._characters = CharacterCultivationService(session_factory)

    def ensure_demo_data(self) -> DemoDataSnapshot:
        data_set_id, created = self._ensure_data_set()
        existing = {
            item.name: item
            for item in self._characters.list_characters(
                data_set_id=data_set_id,
                include_archived=True,
            )
        }
        seeds = (
            CreateCharacter(
                name="示範輸出角色",
                element="虛構屬性",
                role="示範輸出",
                priority=1,
                level=70,
                notes="公開虛構範例, 不代表任何真實帳號或遊戲 Tier。",
            ),
            CreateCharacter(
                name="示範支援角色",
                element="虛構屬性",
                role="示範支援",
                priority=2,
                level=60,
                notes="公開虛構範例, 可隨時清除。",
            ),
        )
        for seed in seeds:
            if seed.name not in existing:
                detail = self._characters.create_character(
                    data_set_id=data_set_id,
                    command=seed,
                )
                existing[seed.name] = detail.summary

        teams = {item.name: item for item in self._characters.list_teams(data_set_id=data_set_id)}
        if "示範養成隊" not in teams:
            self._characters.create_team(
                data_set_id=data_set_id,
                command=CreateTeam(
                    name="示範養成隊",
                    priority=1,
                    notes="僅展示本機隊伍管理, 不是角色強度建議。",
                ),
                members=(
                    TeamMemberInput(existing["示範輸出角色"].id, 1, "輸出"),
                    TeamMemberInput(existing["示範支援角色"].id, 2, "支援"),
                ),
            )

        return DemoDataSnapshot(
            data_set_id=data_set_id,
            character_count=len(
                self._characters.list_characters(
                    data_set_id=data_set_id,
                    include_archived=True,
                )
            ),
            team_count=len(self._characters.list_teams(data_set_id=data_set_id)),
            created_new_dataset=created,
        )

    def clear_demo_data(self) -> int:
        """Delete every demo partition; the query cannot select primary data."""

        with self._session_factory.begin() as session:
            records = tuple(
                session.scalars(select(DataSetRecord).where(DataSetRecord.kind == "demo")).all()
            )
            for record in records:
                session.delete(record)
            session.flush()
            return len(records)

    def _ensure_data_set(self) -> tuple[str, bool]:
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(DataSetRecord).where(
                    DataSetRecord.kind == "demo",
                    DataSetRecord.name == DEMO_DATA_SET_NAME,
                )
            )
            if record is not None:
                return record.id, False
            record = DataSetRecord(
                name=DEMO_DATA_SET_NAME,
                kind="demo",
                is_active=False,
            )
            session.add(record)
            session.flush()
            return record.id, True


class OnboardingService:
    """Finish first-run setup and restore its versioned local settings state."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: SettingsService,
        *,
        data_path: Path,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._data_path = data_path.expanduser().resolve()
        self._now = now
        self._demo = DemoDataService(session_factory)

    def load_state(self) -> OnboardingState:
        return _parse_state(self._settings.get(ONBOARDING_STATE_KEY))

    def complete(self, command: OnboardingCommand) -> OnboardingState:
        if not isinstance(command, OnboardingCommand):
            raise ValueError("command must be OnboardingCommand")
        confirmed_path = command.data_path.expanduser().resolve()
        if confirmed_path != self._data_path:
            raise ValueError("confirmed data_path does not match the application data path")
        completed_at = _aware(self._now()).astimezone(UTC)
        character_id: str | None = None
        if command.mode is OnboardingMode.DEMO_DATA:
            data_set_id = self._demo.ensure_demo_data().data_set_id
        else:
            data_set_id = EchoHistoryService(self._session_factory).ensure_active_primary_dataset()
            if command.mode is OnboardingMode.FIRST_CHARACTER:
                character_id = self._ensure_first_character(
                    data_set_id,
                    (command.character_name or "").strip(),
                )
        state = OnboardingState(
            completed=True,
            completed_at=completed_at,
            monitor_identifier=command.monitor_identifier.strip(),
            data_path=self._data_path,
            mode=command.mode,
            data_set_id=data_set_id,
            character_id=character_id,
            scoring_template_kind=command.scoring_template_kind,
        )
        self._settings.set(CAPTURE_MONITOR_SETTING_KEY, state.monitor_identifier)
        self._settings.set(SCORING_TEMPLATE_SETTING_KEY, state.scoring_template_kind)
        self._settings.set(DISPLAY_MODE_KEY, command.display_mode)
        self._settings.set(RESOLUTION_KEY, command.resolution)
        self._settings.set(DISPLAY_SETUP_KEY, True)
        # The completion marker is written last. If the process stops between
        # settings transactions, the restart safely re-runs idempotent setup.
        self._settings.set(ONBOARDING_STATE_KEY, _state_payload(state))
        return state

    def reset_completion(self) -> bool:
        return self._settings.delete(ONBOARDING_STATE_KEY)

    def _ensure_first_character(self, data_set_id: str, name: str) -> str:
        characters = CharacterCultivationService(self._session_factory)
        existing = next(
            (
                item
                for item in characters.list_characters(
                    data_set_id=data_set_id,
                    include_archived=True,
                )
                if item.name == name
            ),
            None,
        )
        if existing is not None:
            return existing.id
        return characters.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter(
                name=name,
                role="待設定",
                notes="由首次啟動建立; 其餘養成資料請由使用者手動確認。",
            ),
        ).summary.id


def _state_payload(state: OnboardingState) -> dict[str, object]:
    assert state.completed_at is not None
    assert state.monitor_identifier is not None
    assert state.data_path is not None
    assert state.mode is not None
    assert state.data_set_id is not None
    return {
        "schema_version": state.schema_version,
        "completed": state.completed,
        "completed_at": state.completed_at.isoformat(),
        "monitor_identifier": state.monitor_identifier,
        "data_path": str(state.data_path),
        "mode": state.mode.value,
        "data_set_id": state.data_set_id,
        "character_id": state.character_id,
        "scoring_template_kind": state.scoring_template_kind,
    }


def _parse_state(value: object) -> OnboardingState:
    if value is None:
        return OnboardingState()
    if not isinstance(value, dict):
        return OnboardingState(invalid_reason="onboarding state is not an object")
    if value.get("schema_version") != ONBOARDING_SCHEMA_VERSION:
        return OnboardingState(invalid_reason="onboarding state version is not supported")
    try:
        if value.get("completed") is not True:
            raise ValueError("onboarding completion flag is not true")
        completed_at = datetime.fromisoformat(_required_text(value, "completed_at"))
        completed_at = _aware(completed_at).astimezone(UTC)
        monitor = _required_text(value, "monitor_identifier")
        data_path = Path(_required_text(value, "data_path"))
        if not data_path.is_absolute():
            raise ValueError("stored data path is not absolute")
        mode = OnboardingMode(_required_text(value, "mode"))
        data_set_id = _required_text(value, "data_set_id")
        raw_character_id = value.get("character_id")
        if raw_character_id is not None and not isinstance(raw_character_id, str):
            raise ValueError("stored character id is invalid")
        template_kind = value.get("scoring_template_kind", "generic_dps")
        if template_kind not in _SCORING_TEMPLATE_KINDS:
            raise ValueError("stored scoring template kind is invalid")
        return OnboardingState(
            completed=True,
            completed_at=completed_at,
            monitor_identifier=monitor,
            data_path=data_path,
            mode=mode,
            data_set_id=data_set_id,
            character_id=raw_character_id,
            scoring_template_kind=str(template_kind),
        )
    except (TypeError, ValueError) as exc:
        return OnboardingState(invalid_reason=str(exc))


def _required_text(value: dict[object, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"stored {key} is invalid")
    return item.strip()


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value


__all__ = [
    "CAPTURE_MONITOR_SETTING_KEY",
    "DEMO_DATA_SET_NAME",
    "ONBOARDING_SCHEMA_VERSION",
    "ONBOARDING_STATE_KEY",
    "SCORING_TEMPLATE_SETTING_KEY",
    "DemoDataService",
    "DemoDataSnapshot",
    "OnboardingCommand",
    "OnboardingMode",
    "OnboardingService",
    "OnboardingState",
]

_SCORING_TEMPLATE_KINDS = frozenset({"generic_dps", "healing_hp", "defense", "energy_requirement"})
