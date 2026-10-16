"""Register explicit screen ownership without guessing position or replacing manual choices."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from wuwa_assistant.storage.orm import EchoObservationRecord, EchoRecord, EchoSessionRecord, utc_now

from .characters import assign_echo_record, ensure_recognized_character
from .echo_duplicates import complete_echo_signature


@dataclass(frozen=True, slots=True)
class EquipmentRegistration:
    character_id: str | None
    created: bool
    changed: bool
    status: str


def assignment_is_recognized(session: Session, echo_id: str) -> bool:
    records = session.scalars(
        select(EchoSessionRecord).where(EchoSessionRecord.echo_id == echo_id)
    ).all()
    return bool(records) and all(
        item.metadata_json.get("equipment_assignment_source") == "recognition" for item in records
    )


def clear_recognized_equipment(session: Session, echo_session: EchoSessionRecord) -> bool:
    """Caller has explicit unequip evidence; preserve any manual assignment."""

    echo = session.get(EchoRecord, echo_session.echo_id)
    if (
        echo is None
        or echo.character_id is None
        or echo.archived_at is not None
        or not assignment_is_recognized(session, echo.id)
    ):
        return False
    echo.character_id = None
    echo.slot_index = None
    echo.equipped_at = None
    if echo.final_status == "equipped":
        echo.final_status = "kept"
    echo.updated_at = utc_now()
    session.flush()
    return True


def register_recognized_equipment(
    session: Session,
    echo_session: EchoSessionRecord,
    *,
    data_set_id: str,
    name: str,
    slot_index: int | None = None,
) -> EquipmentRegistration:
    """Caller supplies strong same-frame owner/optional slot evidence; never commits."""

    if slot_index is not None and (type(slot_index) is not int or not 1 <= slot_index <= 5):
        raise ValueError("slot_index must be within [1, 5]")
    echo = session.get(EchoRecord, echo_session.echo_id)
    if echo is None or echo.data_set_id != data_set_id:
        raise ValueError("echo does not belong to the requested data set")
    if echo.archived_at is not None or echo_session.was_consumed or echo.final_status == "consumed":
        return EquipmentRegistration(None, False, False, "unavailable")
    character, created = ensure_recognized_character(session, data_set_id=data_set_id, name=name)
    if character is None:
        return EquipmentRegistration(None, False, False, "archived_character")
    if echo.character_id is not None and not assignment_is_recognized(session, echo.id):
        return EquipmentRegistration(character.id, created, False, "manual_assignment")
    status = "registered" if slot_index is not None else "position_unknown"
    if slot_index is None and echo.character_id == character.id:
        # A later unreadable slot does not erase an earlier confirmed position.
        slot_index = echo.slot_index
    if slot_index is None:
        slot_index = _confirmed_rescan_slot(session, echo, character.id)
        if slot_index is not None:
            status = "registered_from_exact_rescan"
            echo_session.metadata_json = {
                **echo_session.metadata_json,
                "equipment_slot_source": "exact_maxed_rescan_same_owner_and_set",
            }
    if slot_index is not None:
        occupied = session.scalar(
            select(EchoRecord).where(
                EchoRecord.character_id == character.id,
                EchoRecord.slot_index == slot_index,
                EchoRecord.archived_at.is_(None),
                EchoRecord.id != echo.id,
            )
        )
        if occupied is not None and not assignment_is_recognized(session, occupied.id):
            slot_index = None
            status = "manual_slot_conflict"
    if (echo.character_id, echo.slot_index, echo.final_status) == (
        character.id,
        slot_index,
        "equipped",
    ):
        return EquipmentRegistration(character.id, created, False, status)
    if slot_index is not None:
        assign_echo_record(
            session,
            character.id,
            slot_index,
            echo.id,
            data_set_id=data_set_id,
            replace=True,
            recognized=True,
        )
    else:
        echo.character_id = character.id
        echo.slot_index = None
        echo.final_status = "equipped"
        echo.equipped_at = utc_now()
        echo.updated_at = utc_now()
        echo_session.metadata_json = {
            **echo_session.metadata_json,
            "equipment_assignment_source": "recognition",
        }
        session.flush()
    return EquipmentRegistration(character.id, created, True, status)


def _confirmed_rescan_slot(session: Session, echo: EchoRecord, character_id: str) -> int | None:
    """Carry a confirmed position across exact, fully tuned rescans only.

    This follows the user's explicit numeric-rescan policy. It does not infer
    physical identity, upgrade/consumption lineage, or fill empty positions.
    A different/unknown set, partial/low-confidence data or conflicting matches
    cannot supply a position. Manual occupied slots remain protected by caller.
    """

    def signature(record: EchoRecord) -> str | None:
        if record.level != 25 or not record.set_name:
            return None
        latest = session.scalar(
            select(EchoObservationRecord)
            .join(EchoSessionRecord, EchoObservationRecord.echo_session_id == EchoSessionRecord.id)
            .where(EchoSessionRecord.echo_id == record.id)
            .order_by(
                EchoObservationRecord.observed_at.desc(), EchoObservationRecord.sequence_no.desc()
            )
            .limit(1)
        )
        if latest is None or latest.requires_review:
            return None
        return complete_echo_signature(
            name=record.name,
            level=record.level,
            cost=record.cost,
            main_stats=record.main_stats_json,
            sub_stats=record.sub_stats_json,
        )

    key = signature(echo)
    if key is None:
        return None
    candidates = session.scalars(
        select(EchoRecord).where(
            EchoRecord.data_set_id == echo.data_set_id,
            EchoRecord.character_id == character_id,
            EchoRecord.slot_index.is_not(None),
            EchoRecord.archived_at.is_(None),
            EchoRecord.final_status == "equipped",
            EchoRecord.set_name == echo.set_name,
            EchoRecord.id != echo.id,
        )
    ).all()
    slots = {item.slot_index for item in candidates if signature(item) == key}
    return slots.pop() if len(slots) == 1 else None
