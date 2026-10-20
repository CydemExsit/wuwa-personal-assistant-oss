"""Bounded, explicit-slot screen evidence for equipment synchronization.

Evidence describes the last accepted read, never unseen current game state.
No images, inferred slots or historical repair are accepted as fresh proof.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from wuwa_assistant.storage.orm import CharacterRecord, EchoRecord

from .character_stats import _observed_stat
from .observation_review import observation_review_issues

if TYPE_CHECKING:
    from .characters import CharacterDetail


def equipment_signature(payload: Mapping[str, Any]) -> str | None:
    """Compare normalized, complete numeric inputs; confidence jitter is ignored."""
    name, set_name = payload.get("name"), payload.get("set_name")
    level, cost = payload.get("level"), payload.get("cost")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(set_name, str)
        or not set_name.strip()
        or type(level) is not int
        or not 0 <= level <= 25
        or type(cost) is not int
        or cost not in {1, 3, 4}
    ):
        return None
    groups = []
    for key, count in (("main_stats", 2), ("sub_stats", level // 5)):
        raw = payload.get(key)
        if not isinstance(raw, (list, tuple)) or len(raw) != count:
            return None
        parsed = [_observed_stat(stat) if isinstance(stat, dict) else None for stat in raw]
        if any(stat is None for stat in parsed):
            return None
        values = sorted((stat[0], str(stat[1].normalize())) for stat in parsed if stat is not None)
        if len({key for key, _ in values}) != count:
            return None
        groups.append(values)
    value = [name.strip(), set_name.strip(), level, cost, groups]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def record_payload(echo: EchoRecord) -> dict[str, Any]:
    return {
        "name": echo.name,
        "set_name": echo.set_name,
        "level": echo.level,
        "cost": echo.cost,
        "main_stats": echo.main_stats_json,
        "sub_stats": echo.sub_stats_json,
    }


def panel_record_signature(echo: EchoRecord) -> str:
    """Also detect changes to incomplete inputs, without reacting to OCR text/jitter."""
    payload = record_payload(echo)
    for group in ("main_stats", "sub_stats"):
        payload[group] = [
            (s.get("name"), s.get("value"), s.get("unit"), _observed_stat(s) is not None)
            for s in payload[group]
        ]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _describe(payload: Mapping[str, Any]) -> str:
    stats = [
        f"{s.get('name', '?')} {s.get('value', '?')}{s.get('unit') or ''}"
        for key in ("main_stats", "sub_stats")
        for s in payload.get(key, ())
        if isinstance(s, dict)
    ]
    return (
        f"{payload.get('set_name') or '套裝待確認'}・Lv.{payload.get('level')}・"
        f"COST {payload.get('cost')}\n" + "、".join(stats)
    )


def record_equipment_read(
    session: Session,
    *,
    character_id: str,
    slot: int,
    capture_id: str,
    payload: Mapping[str, Any],
    when: datetime,
    requires_review: bool,
) -> bool:
    character = session.get(CharacterRecord, character_id)
    if character is None:
        return False
    goal = dict(character.cultivation_goal_json or {})
    check = goal.get("equipment_check", {})
    stamp = when.isoformat()
    # A reset is a wall-clock boundary, including in-flight older observations.
    if check.get("started_at", "") > stamp:
        return False
    if check.get("capture_id") != capture_id:
        check = {"version": 1, "started_at": stamp, "capture_id": capture_id, "slots": {}}
    slots = dict(check.get("slots", {}))
    old = slots.get(str(slot), {})
    if old.get("observed_at", "") > stamp:
        return False
    echo = session.scalar(
        select(EchoRecord).where(
            EchoRecord.character_id == character_id,
            EchoRecord.slot_index == slot,
            EchoRecord.archived_at.is_(None),
        )
    )
    observed = equipment_signature(payload)
    saved = equipment_signature(record_payload(echo)) if echo is not None else None
    status = (
        "review"
        if requires_review or observed is None or observation_review_issues(payload)
        else ("matched" if saved == observed else "conflict")
    )
    item = {
        "status": status,
        "observed_signature": observed,
        "tool_signature": saved,
        "tool_echo_id": echo.id if echo is not None else None,
        "observed_name": payload.get("name"),
        "observed_details": _describe(payload),
        "observed_at": stamp,
    }
    # Repeated stable frames must not queue a workspace/scoring reload each time.
    if {k: v for k, v in old.items() if k != "observed_at"} == {
        k: v for k, v in item.items() if k != "observed_at"
    }:
        return False
    slots[str(slot)] = item
    goal["equipment_check"] = {**check, "slots": slots}
    character.cultivation_goal_json = goal
    return True


@dataclass(frozen=True, slots=True)
class EquipmentCheck:
    matched: int
    labels: tuple[str, ...]
    details: str


def equipment_check(detail: CharacterDetail) -> EquipmentCheck:
    check = detail.cultivation_goal.get("equipment_check", {})
    evidence = check.get("slots", {}) if isinstance(check, dict) else {}
    labels, lines = [], ["只代表最近一次逐格讀取;遊戲換裝後請重新核對五格。"]
    matched = 0
    for slot in detail.echo_slots:
        item = evidence.get(str(slot.slot_index), {})
        payload = {
            "name": slot.name,
            "set_name": slot.set_name,
            "level": slot.level,
            "cost": slot.cost,
            "main_stats": slot.main_stats,
            "sub_stats": slot.sub_stats,
        }
        current = equipment_signature(payload) if slot.echo_id else None
        state = item.get("status")
        if item and (
            item.get("tool_echo_id") != slot.echo_id or item.get("tool_signature") != current
        ):
            label = "待重讀"
        elif state == "matched" and current is not None:
            label = "詞條相符"
            matched += 1
        elif state == "conflict":
            label = "有差異"
        elif state == "review":
            label = "待確認"
        else:
            label = "未讀取"
        labels.append(f"{slot.slot_index} {label}")
        lines.append(
            f"位置{slot.slot_index}・{label}\n工具:{slot.name or '未裝備'}\n{_describe(payload)}\n"
            f"最近讀取:{item.get('observed_name') or '無'}・"
            f"{item.get('observed_at') or '尚未讀取'}\n"
            f"{item.get('observed_details') or ''}"
        )
    lines.append(
        "請在遊戲聲骸選擇頁逐格停留,讓工具讀到角色、位置與完整詞條。"
        "手動裝備有衝突時會保留,請核對後自行調整。"
    )
    return EquipmentCheck(matched, tuple(labels), "\n\n".join(lines))
