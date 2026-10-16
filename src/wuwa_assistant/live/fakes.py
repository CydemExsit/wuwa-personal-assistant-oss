"""Deterministic live analyzer for integration tests and offline demonstrations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock

from wuwa_assistant.capture.roi_packet import RoiPacket

from .dto import AnalysisResult


class FakeRoiAnalyzer:
    """Return configured results by a deterministic packet key and record calls."""

    def __init__(
        self,
        responses: Mapping[str, AnalysisResult],
        *,
        default: AnalysisResult | None = None,
        response_key: Callable[[RoiPacket], str] | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._default = default
        self._response_key = response_key or _default_response_key
        self._calls: list[str] = []
        self._lock = Lock()

    @property
    def calls(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._calls)

    def analyze(self, packet: RoiPacket) -> AnalysisResult:
        key = self._response_key(packet)
        with self._lock:
            self._calls.append(key)
        result = self._responses.get(key, self._default)
        if result is None:
            raise LookupError(f"No fake analyzer response for packet key {key!r}")
        return result


def _default_response_key(packet: RoiPacket) -> str:
    return packet.source_file or f"{packet.kind.value}:{packet.page_type or 'unclassified'}"
