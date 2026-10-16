"""Typed domain models used by parsers, state tracking, and the CLI."""

from .domain import (
    EchoEvent,
    EchoObservation,
    EchoSession,
    EchoStat,
    EventType,
    OcrToken,
    PageClassification,
    PageType,
    SelectionIdentityEvidence,
    SessionState,
    StatKind,
    to_jsonable,
)

__all__ = [
    "EchoEvent",
    "EchoObservation",
    "EchoSession",
    "EchoStat",
    "EventType",
    "OcrToken",
    "PageClassification",
    "PageType",
    "SelectionIdentityEvidence",
    "SessionState",
    "StatKind",
    "to_jsonable",
]
