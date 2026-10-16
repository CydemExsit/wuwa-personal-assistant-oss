"""Typed inputs and transparent results for character cultivation completion."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite


class CultivationSection(StrEnum):
    CHARACTER = "character"
    WEAPON = "weapon"
    SKILLS = "skills"
    SMALL_NODES = "small_nodes"
    ECHOES = "echoes"


class SkillPriority(StrEnum):
    CORE = "core"
    SECONDARY = "secondary"


class EchoSlotStatus(StrEnum):
    MISSING = "missing"
    NEEDS_CONFIRMATION = "needs_confirmation"
    INVALID = "invalid"
    BELOW_THRESHOLD = "below_threshold"
    PASSING = "passing"


class BlockerCode(StrEnum):
    CHARACTER_LEVEL = "character_level"
    WEAPON_MISSING = "weapon_missing"
    WEAPON_LEVEL = "weapon_level"
    WEAPON_RANK = "weapon_rank"
    SKILLS_MISSING = "skills_missing"
    SKILL_LEVEL = "skill_level"
    SMALL_NODE_INCOMPLETE = "small_node_incomplete"
    SMALL_NODE_UNKNOWN = "small_node_unknown"
    ECHO_MISSING = "echo_missing"
    ECHO_SET_UNKNOWN = "echo_set_unknown"
    ECHO_SET_INVALID = "echo_set_invalid"
    ECHO_MAIN_STAT_UNKNOWN = "echo_main_stat_unknown"
    ECHO_MAIN_STAT_INVALID = "echo_main_stat_invalid"
    ECHO_SCORE_MISSING = "echo_score_missing"
    ECHO_SCORE_BELOW_THRESHOLD = "echo_score_below_threshold"


@dataclass(frozen=True, slots=True)
class ProgressTarget:
    current: int
    target: int

    def __post_init__(self) -> None:
        if self.current < 0:
            raise ValueError("current progress must be non-negative")
        if self.target <= 0:
            raise ValueError("progress target must be positive")


@dataclass(frozen=True, slots=True)
class WeaponProgress:
    name: str
    level: ProgressTarget
    rank: ProgressTarget

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("weapon name must not be empty")


@dataclass(frozen=True, slots=True)
class SkillProgress:
    name: str
    current_level: int
    priority: SkillPriority
    target_level: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("skill name must not be empty")
        if self.current_level < 0:
            raise ValueError("skill level must be non-negative")
        if not isinstance(self.priority, SkillPriority):
            raise TypeError("skill priority must be a SkillPriority")
        if self.target_level is not None and self.target_level <= 0:
            raise ValueError("skill target must be positive")


@dataclass(frozen=True, slots=True)
class EchoSlotProgress:
    """One permanent character echo slot.

    ``None`` validity values mean the field has not been confirmed. They must
    never be treated as valid. Empty slots deliberately carry no echo fields.
    """

    slot_index: int
    is_filled: bool
    set_is_valid: bool | None = None
    main_stat_is_valid: bool | None = None
    score: float | None = None
    echo_id: str | None = None

    def __post_init__(self) -> None:
        if self.slot_index <= 0:
            raise ValueError("echo slot index must be positive")
        if type(self.is_filled) is not bool:
            raise TypeError("echo filled state must be bool")
        if any(
            value is not None and type(value) is not bool
            for value in (self.set_is_valid, self.main_stat_is_valid)
        ):
            raise TypeError("echo validity states must be bool or None")
        if self.score is not None and not 0 <= self.score <= 100:
            raise ValueError("echo score must be between zero and one hundred")
        if not self.is_filled and any(
            value is not None
            for value in (self.set_is_valid, self.main_stat_is_valid, self.score, self.echo_id)
        ):
            raise ValueError("empty echo slots must not contain echo data")
        if self.echo_id is not None and not self.echo_id.strip():
            raise ValueError("echo id must not be blank")


@dataclass(frozen=True, slots=True)
class SectionWeights:
    character: float = 1.0
    weapon: float = 1.0
    skills: float = 1.0
    small_nodes: float = 1.0
    echoes: float = 1.0

    def __post_init__(self) -> None:
        values = tuple(self.as_dict().values())
        if any(not isfinite(value) for value in values):
            raise ValueError("section weights must be finite")
        if any(value < 0 for value in values):
            raise ValueError("section weights must be non-negative")
        if sum(values) <= 0:
            raise ValueError("at least one section weight must be positive")

    def as_dict(self) -> dict[CultivationSection, float]:
        return {
            CultivationSection.CHARACTER: self.character,
            CultivationSection.WEAPON: self.weapon,
            CultivationSection.SKILLS: self.skills,
            CultivationSection.SMALL_NODES: self.small_nodes,
            CultivationSection.ECHOES: self.echoes,
        }


@dataclass(frozen=True, slots=True)
class CultivationRules:
    core_skill_target: int = 10
    secondary_skill_target: int = 6
    required_small_nodes: int = 10
    required_echo_slots: int = 5
    echo_passing_score: float = 70.0
    section_weights: SectionWeights = field(default_factory=SectionWeights)

    def __post_init__(self) -> None:
        if self.core_skill_target <= 0 or self.secondary_skill_target <= 0:
            raise ValueError("default skill targets must be positive")
        if self.required_small_nodes <= 0:
            raise ValueError("required small node count must be positive")
        if self.required_echo_slots <= 0:
            raise ValueError("required echo slot count must be positive")
        if not 0 < self.echo_passing_score <= 100:
            raise ValueError("echo passing score must be above zero and at most one hundred")

    def target_for_skill(self, skill: SkillProgress) -> int:
        if skill.target_level is not None:
            return skill.target_level
        if skill.priority is SkillPriority.CORE:
            return self.core_skill_target
        return self.secondary_skill_target


@dataclass(frozen=True, slots=True)
class CharacterCultivationInput:
    character_name: str
    level: ProgressTarget
    weapon: WeaponProgress | None
    skills: tuple[SkillProgress, ...]
    small_nodes: tuple[bool | None, ...]
    echo_slots: tuple[EchoSlotProgress, ...]

    def __post_init__(self) -> None:
        if not self.character_name.strip():
            raise ValueError("character name must not be empty")
        for value in self.small_nodes:
            if value is not None and type(value) is not bool:
                raise TypeError("small node states must be bool or None")


@dataclass(frozen=True, slots=True)
class CompletionComponent:
    key: str
    label: str
    current: float
    target: float
    percent: float
    at_goal: bool


@dataclass(frozen=True, slots=True)
class SectionCompletion:
    section: CultivationSection
    percent: float
    at_goal: bool
    components: tuple[CompletionComponent, ...]


@dataclass(frozen=True, slots=True)
class EchoSlotAssessment:
    slot_index: int
    status: EchoSlotStatus
    readiness_percent: float
    is_passing: bool
    score: float | None
    passing_score: float
    issues: tuple[BlockerCode, ...]


@dataclass(frozen=True, slots=True)
class CultivationBlocker:
    code: BlockerCode
    section: CultivationSection
    message_zh_tw: str
    item_key: str | None = None
    slot_index: int | None = None


@dataclass(frozen=True, slots=True)
class CultivationCompletion:
    character_name: str
    sections: tuple[SectionCompletion, ...]
    echo_slots: tuple[EchoSlotAssessment, ...]
    echo_passing_count: int
    echo_required_count: int
    weakest_echo_slot: int
    blockers: tuple[CultivationBlocker, ...]
    overall_percent: float
    at_goal: bool

    def section(self, key: CultivationSection) -> SectionCompletion:
        for result in self.sections:
            if result.section is key:
                return result
        raise KeyError(key)


__all__ = [
    "BlockerCode",
    "CharacterCultivationInput",
    "CompletionComponent",
    "CultivationBlocker",
    "CultivationCompletion",
    "CultivationRules",
    "CultivationSection",
    "EchoSlotAssessment",
    "EchoSlotProgress",
    "EchoSlotStatus",
    "ProgressTarget",
    "SectionCompletion",
    "SectionWeights",
    "SkillPriority",
    "SkillProgress",
    "WeaponProgress",
]
