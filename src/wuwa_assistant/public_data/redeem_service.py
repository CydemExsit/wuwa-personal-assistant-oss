"""Independent public-code cache and partial-source failure handling."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .redeem_models import COMMUNITY_URL, RedeemCode, RedeemSnapshot
from .redeem_sources import fetch_community, fetch_official


class RedeemCodeService:
    def __init__(
        self,
        directory: Path,
        *,
        official: Callable[[], tuple[RedeemCode, ...]] = fetch_official,
        community: Callable[[], tuple[RedeemCode, ...]] = fetch_community,
    ) -> None:
        self.path = directory / "redeem-codes.json"
        self._sources = (("官方公告", official), ("公開彙整", community))

    def load_cached(self) -> RedeemSnapshot:
        try:
            if self.path.stat().st_size > 1_000_000:
                raise ValueError("cache too large")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw["schema_version"] != 1 or not isinstance(raw["codes"], list):
                raise ValueError("invalid code cache")
            if len(raw["codes"]) > 500:
                raise ValueError("too many codes")
            stamp = datetime.fromisoformat(raw["checked_at"])
            if stamp.tzinfo is None:
                raise ValueError("invalid timestamp")
            codes = tuple(RedeemCode(**item) for item in raw["codes"])
            return RedeemSnapshot(codes, stamp.isoformat(), "已載入兌換碼快取; 有效性以遊戲為準。")
        except (OSError, ValueError, TypeError, KeyError):
            return RedeemSnapshot()

    def synchronize(self) -> RedeemSnapshot:
        cached = self.load_cached()
        merged = {c.key: c for c in cached.codes}
        failures: list[str] = []
        successes = 0
        checked: set[str] = set()
        for name, source in self._sources:
            try:
                incoming = source()
                if len(incoming) > 500:
                    raise ValueError("too many source codes")
                successes += 1
                keys = {c.key for c in incoming}
                checked.update(keys)
                for key, old in tuple(merged.items()):
                    belongs = (old.source_url == COMMUNITY_URL) == (name == "公開彙整")
                    if belongs and key not in keys and old.available:
                        merged[key] = replace(old, status="unlisted")
                for code in incoming:
                    # The community source runs last and has explicit active/expired lists.
                    existing = merged.get(code.key)
                    if (
                        name == "官方公告"
                        and existing is not None
                        and existing.source_url == COMMUNITY_URL
                    ):
                        continue
                    merged[code.key] = code
            except (OSError, ValueError, RuntimeError):
                failures.append(name)
        if successes == 0:
            return replace(cached, message="更新失敗 (官方公告、公開彙整); 保留上次快取。")
        codes = tuple(sorted(merged.values(), key=lambda c: (not c.available, c.key)))[:500]
        message = "已檢查兌換碼。有效性與期限以遊戲及來源公告為準。"
        if failures:
            message += f" {'、'.join(failures)}更新失敗, 該來源保留快取。"
        snapshot = RedeemSnapshot(
            codes,
            datetime.now(UTC).isoformat(timespec="seconds"),
            message,
            True,
            tuple(sorted(checked)),
        )
        try:
            self._save(snapshot)
        except OSError:
            snapshot = replace(snapshot, message=message + " 本機快取寫入失敗。")
        return snapshot

    def _save(self, snapshot: RedeemSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="redeem-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": 1,
                        "checked_at": snapshot.checked_at,
                        "codes": [asdict(c) for c in snapshot.codes],
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
