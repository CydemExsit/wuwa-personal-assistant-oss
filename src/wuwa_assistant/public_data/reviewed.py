"""Human-verified official image notices; textual updates take precedence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from functools import lru_cache
from importlib.resources import files

from .client import official_article_url
from .models import OfficialSchedule


@lru_cache(maxsize=1)
def reviewed_schedules() -> tuple[OfficialSchedule, ...]:
    payload = json.loads(
        files("wuwa_assistant.data.planning")
        .joinpath("reviewed-schedules.v1.json")
        .read_text(encoding="utf-8")
    )
    result: list[OfficialSchedule] = []
    for row in payload["schedules"]:
        end = datetime.fromisoformat(row["ends_at"])
        identity = f"{row['kind']}|{row['title']}|{end.isoformat()}"
        result.append(
            OfficialSchedule(
                key="official:" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                title=row["title"],
                kind=row["kind"],
                official_url=official_article_url(5516),
                starts_at=datetime.fromisoformat(row["starts_at"]),
                ends_at=end,
                five_star_characters=tuple(row.get("five_star_characters", [])),
                four_star_characters=tuple(row.get("four_star_characters", [])),
                start_label=row["starts_at"],
                end_label=row["ends_at"],
                version="3.7",
                bonus_kind=row.get("bonus_kind"),
                bonus_scopes=tuple(row.get("bonus_scopes", [])),
            )
        )
    return tuple(result)
