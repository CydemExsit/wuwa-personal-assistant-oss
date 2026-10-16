"""Opt-in, read-only public catalog updates with atomic offline fallback."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .tacet_catalog import CATALOG_NAME, load_tacet_catalog, parse_tacet_catalog
from .tacet_source import SOURCE_PATHS, SOURCE_REPOSITORY, build_tacet_catalog_payload

BRANCH_URL = f"https://api.github.com/repos/{SOURCE_REPOSITORY}/branches/master"


def fetch_public_source(url: str, maximum: int) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "api.github.com",
        "raw.githubusercontent.com",
    }:
        raise ValueError("無音區來源網址無效。")
    request = Request(url, headers={"User-Agent": "WuwaPersonalAssistant-public-data"})
    with urlopen(request, timeout=30) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.netloc != parsed.netloc:
            raise ValueError("無音區來源發生未預期的轉址。")
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("無音區來源超過大小限制。")
    return body


class TacetCatalogSync:
    def __init__(
        self,
        directory: Path,
        *,
        fetch: Callable[[str, int], bytes] = fetch_public_source,
    ) -> None:
        self.directory = directory
        self.fetch = fetch

    def synchronize(self) -> str:
        """Check a tiny revision first; download tables only after a revision change."""
        current = load_tacet_catalog(self.directory)
        try:
            branch = json.loads(self.fetch(BRANCH_URL, 128 * 1024))
            revision = branch["commit"]["sha"]
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise ValueError("公開來源版本無效。")
            if revision == current.source_revision:
                return f"無音區資料 {current.game_version} 已是來源目前版本。"
            sources = {
                key: self.fetch(
                    f"https://raw.githubusercontent.com/{SOURCE_REPOSITORY}/{revision}/{path}",
                    64 * 1024 * 1024 if key.startswith("text") else 2 * 1024 * 1024,
                )
                for key, path in SOURCE_PATHS.items()
            }
            payload = build_tacet_catalog_payload(
                sources,
                revision=revision,
                retrieved_on=date.today().isoformat(),
                previous_aliases=dict(current.aliases),
            )
            updated = parse_tacet_catalog(payload)
            if tuple(map(int, updated.game_version.split("."))) < tuple(
                map(int, current.game_version.split("."))
            ):
                raise ValueError("來源版本較舊。")
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.directory / CATALOG_NAME)
            finally:
                temporary.unlink(missing_ok=True)
            return f"已更新無音區資料 {updated.game_version}, 共 {len(updated.fields)} 個。"
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return f"無音區更新未完成 ({type(exc).__name__}); 保留 {current.game_version} 資料。"
