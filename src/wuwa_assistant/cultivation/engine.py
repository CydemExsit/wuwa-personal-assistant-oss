"""Deterministic character cultivation completion calculations."""

from __future__ import annotations

from collections.abc import Iterable

from .models import (
    BlockerCode,
    CharacterCultivationInput,
    CompletionComponent,
    CultivationBlocker,
    CultivationCompletion,
    CultivationRules,
    CultivationSection,
    EchoSlotAssessment,
    EchoSlotProgress,
    EchoSlotStatus,
    SectionCompletion,
)


def evaluate_cultivation(
    progress: CharacterCultivationInput,
    rules: CultivationRules | None = None,
) -> CultivationCompletion:
    """Evaluate one character without database or UI dependencies."""

    active_rules = rules or CultivationRules()
    _validate_collections(progress, active_rules)
    blockers: list[CultivationBlocker] = []

    character = _character_section(progress, blockers)
    weapon = _weapon_section(progress, blockers)
    skills = _skills_section(progress, active_rules, blockers)
    nodes = _node_section(progress, active_rules, blockers)
    echoes, echo_assessments = _echo_section(progress, active_rules, blockers)
    sections = (character, weapon, skills, nodes, echoes)

    weights = active_rules.section_weights.as_dict()
    total_weight = sum(weights.values())
    overall = sum(item.percent * weights[item.section] for item in sections) / total_weight
    passing_count = sum(item.is_passing for item in echo_assessments)
    weakest = min(echo_assessments, key=lambda item: (item.readiness_percent, item.slot_index))
    return CultivationCompletion(
        character_name=progress.character_name,
        sections=sections,
        echo_slots=echo_assessments,
        echo_passing_count=passing_count,
        echo_required_count=active_rules.required_echo_slots,
        weakest_echo_slot=weakest.slot_index,
        blockers=tuple(blockers),
        overall_percent=round(overall, 2),
        at_goal=all(item.at_goal for item in sections) and not blockers,
    )


def _validate_collections(
    progress: CharacterCultivationInput,
    rules: CultivationRules,
) -> None:
    skill_names = [skill.name.strip().casefold() for skill in progress.skills]
    if len(skill_names) != len(set(skill_names)):
        raise ValueError("skill names must be unique")
    if len(progress.small_nodes) > rules.required_small_nodes:
        raise ValueError("small node state count exceeds configured required count")
    slot_indices = [slot.slot_index for slot in progress.echo_slots]
    if len(slot_indices) != len(set(slot_indices)):
        raise ValueError("echo slot indices must be unique")
    if any(index > rules.required_echo_slots for index in slot_indices):
        raise ValueError("echo slot index exceeds configured required count")


def _character_section(
    progress: CharacterCultivationInput,
    blockers: list[CultivationBlocker],
) -> SectionCompletion:
    component = _component(
        "character_level",
        "角色等級",
        progress.level.current,
        progress.level.target,
    )
    if not component.at_goal:
        blockers.append(
            CultivationBlocker(
                BlockerCode.CHARACTER_LEVEL,
                CultivationSection.CHARACTER,
                f"角色等級 {progress.level.current}/{progress.level.target}",
                item_key=component.key,
            )
        )
    return _section(CultivationSection.CHARACTER, (component,))


def _weapon_section(
    progress: CharacterCultivationInput,
    blockers: list[CultivationBlocker],
) -> SectionCompletion:
    weapon = progress.weapon
    if weapon is None:
        blockers.append(
            CultivationBlocker(
                BlockerCode.WEAPON_MISSING,
                CultivationSection.WEAPON,
                "尚未設定武器",
            )
        )
        return _section(
            CultivationSection.WEAPON,
            (
                CompletionComponent("weapon_level", "武器等級", 0, 1, 0.0, False),
                CompletionComponent("weapon_rank", "武器諧振階級", 0, 1, 0.0, False),
            ),
        )

    level = _component("weapon_level", "武器等級", weapon.level.current, weapon.level.target)
    rank = _component("weapon_rank", "武器諧振階級", weapon.rank.current, weapon.rank.target)
    if not level.at_goal:
        blockers.append(
            CultivationBlocker(
                BlockerCode.WEAPON_LEVEL,
                CultivationSection.WEAPON,
                f"武器等級 {weapon.level.current}/{weapon.level.target}",
                item_key=level.key,
            )
        )
    if not rank.at_goal:
        blockers.append(
            CultivationBlocker(
                BlockerCode.WEAPON_RANK,
                CultivationSection.WEAPON,
                f"武器諧振階級 {weapon.rank.current}/{weapon.rank.target}",
                item_key=rank.key,
            )
        )
    return _section(CultivationSection.WEAPON, (level, rank))


def _skills_section(
    progress: CharacterCultivationInput,
    rules: CultivationRules,
    blockers: list[CultivationBlocker],
) -> SectionCompletion:
    if not progress.skills:
        blockers.append(
            CultivationBlocker(
                BlockerCode.SKILLS_MISSING,
                CultivationSection.SKILLS,
                "尚未設定技能進度",
            )
        )
        return SectionCompletion(CultivationSection.SKILLS, 0.0, False, ())

    components: list[CompletionComponent] = []
    for index, skill in enumerate(progress.skills, start=1):
        target = rules.target_for_skill(skill)
        component = _component(
            f"skill-{index}",
            skill.name,
            skill.current_level,
            target,
        )
        components.append(component)
        if not component.at_goal:
            blockers.append(
                CultivationBlocker(
                    BlockerCode.SKILL_LEVEL,
                    CultivationSection.SKILLS,
                    f"{skill.name} {skill.current_level}/{target}",
                    item_key=skill.name,
                )
            )
    return _section(CultivationSection.SKILLS, tuple(components))


def _node_section(
    progress: CharacterCultivationInput,
    rules: CultivationRules,
    blockers: list[CultivationBlocker],
) -> SectionCompletion:
    missing_count = rules.required_small_nodes - len(progress.small_nodes)
    states = progress.small_nodes + (None,) * missing_count
    components: list[CompletionComponent] = []
    for index, state in enumerate(states, start=1):
        component = CompletionComponent(
            key=f"small-node-{index}",
            label=f"小型屬性節點 {index}",
            current=1.0 if state is True else 0.0,
            target=1.0,
            percent=100.0 if state is True else 0.0,
            at_goal=state is True,
        )
        components.append(component)
        if state is not True:
            unknown = state is None
            blockers.append(
                CultivationBlocker(
                    BlockerCode.SMALL_NODE_UNKNOWN
                    if unknown
                    else BlockerCode.SMALL_NODE_INCOMPLETE,
                    CultivationSection.SMALL_NODES,
                    f"小型屬性節點 {index}{'尚未確認' if unknown else '尚未完成'}",
                    item_key=component.key,
                )
            )
    return _section(CultivationSection.SMALL_NODES, tuple(components))


def _echo_section(
    progress: CharacterCultivationInput,
    rules: CultivationRules,
    blockers: list[CultivationBlocker],
) -> tuple[SectionCompletion, tuple[EchoSlotAssessment, ...]]:
    supplied = {slot.slot_index: slot for slot in progress.echo_slots}
    assessments = tuple(
        _assess_echo_slot(
            supplied.get(index, EchoSlotProgress(index, is_filled=False)),
            rules.echo_passing_score,
            blockers,
        )
        for index in range(1, rules.required_echo_slots + 1)
    )
    components = tuple(
        CompletionComponent(
            key=f"echo-slot-{item.slot_index}",
            label=f"永久聲骸位置 {item.slot_index}",
            current=item.readiness_percent,
            target=100.0,
            percent=item.readiness_percent,
            at_goal=item.is_passing,
        )
        for item in assessments
    )
    return _section(CultivationSection.ECHOES, components), assessments


def _assess_echo_slot(
    slot: EchoSlotProgress,
    passing_score: float,
    blockers: list[CultivationBlocker],
) -> EchoSlotAssessment:
    issues: list[BlockerCode] = []
    if not slot.is_filled:
        issues.append(BlockerCode.ECHO_MISSING)
    else:
        if slot.set_is_valid is None:
            issues.append(BlockerCode.ECHO_SET_UNKNOWN)
        elif not slot.set_is_valid:
            issues.append(BlockerCode.ECHO_SET_INVALID)
        if slot.main_stat_is_valid is None:
            issues.append(BlockerCode.ECHO_MAIN_STAT_UNKNOWN)
        elif not slot.main_stat_is_valid:
            issues.append(BlockerCode.ECHO_MAIN_STAT_INVALID)
        if slot.score is None:
            issues.append(BlockerCode.ECHO_SCORE_MISSING)
        elif slot.score < passing_score:
            issues.append(BlockerCode.ECHO_SCORE_BELOW_THRESHOLD)

    for issue in issues:
        blockers.append(
            CultivationBlocker(
                issue,
                CultivationSection.ECHOES,
                _echo_blocker_message(issue, slot.slot_index, slot.score, passing_score),
                item_key=f"echo-slot-{slot.slot_index}",
                slot_index=slot.slot_index,
            )
        )

    is_valid = slot.set_is_valid is True and slot.main_stat_is_valid is True
    is_passing = is_valid and slot.score is not None and slot.score >= passing_score
    readiness = (
        min(100.0, slot.score / passing_score * 100) if is_valid and slot.score is not None else 0.0
    )
    if not slot.is_filled:
        status = EchoSlotStatus.MISSING
    elif slot.set_is_valid is False or slot.main_stat_is_valid is False:
        status = EchoSlotStatus.INVALID
    elif slot.set_is_valid is None or slot.main_stat_is_valid is None or slot.score is None:
        status = EchoSlotStatus.NEEDS_CONFIRMATION
    elif is_passing:
        status = EchoSlotStatus.PASSING
    else:
        status = EchoSlotStatus.BELOW_THRESHOLD
    return EchoSlotAssessment(
        slot_index=slot.slot_index,
        status=status,
        readiness_percent=round(readiness, 2),
        is_passing=is_passing,
        score=slot.score,
        passing_score=passing_score,
        issues=tuple(issues),
    )


def _echo_blocker_message(
    code: BlockerCode,
    slot_index: int,
    score: float | None,
    passing_score: float,
) -> str:
    messages = {
        BlockerCode.ECHO_MISSING: f"永久聲骸位置 {slot_index} 尚未裝備",
        BlockerCode.ECHO_SET_UNKNOWN: f"永久聲骸位置 {slot_index} 的套裝尚未確認",
        BlockerCode.ECHO_SET_INVALID: f"永久聲骸位置 {slot_index} 的套裝不符合目標",
        BlockerCode.ECHO_MAIN_STAT_UNKNOWN: f"永久聲骸位置 {slot_index} 的主詞條尚未確認",
        BlockerCode.ECHO_MAIN_STAT_INVALID: f"永久聲骸位置 {slot_index} 的主詞條不符合目標",
        BlockerCode.ECHO_SCORE_MISSING: f"永久聲骸位置 {slot_index} 尚未評分",
        BlockerCode.ECHO_SCORE_BELOW_THRESHOLD: (
            f"永久聲骸位置 {slot_index} 分數 {score:g}/{passing_score:g} 未達及格"
            if score is not None
            else f"永久聲骸位置 {slot_index} 未達及格"
        ),
    }
    return messages[code]


def _component(
    key: str,
    label: str,
    current: int,
    target: int,
) -> CompletionComponent:
    percent = min(100.0, current / target * 100)
    return CompletionComponent(
        key=key,
        label=label,
        current=float(current),
        target=float(target),
        percent=round(percent, 2),
        at_goal=current >= target,
    )


def _section(
    key: CultivationSection,
    components: Iterable[CompletionComponent],
) -> SectionCompletion:
    items = tuple(components)
    if not items:
        return SectionCompletion(key, 0.0, False, ())
    percent = sum(item.percent for item in items) / len(items)
    return SectionCompletion(
        section=key,
        percent=round(percent, 2),
        at_goal=all(item.at_goal for item in items),
        components=items,
    )


__all__ = ["evaluate_cultivation"]
