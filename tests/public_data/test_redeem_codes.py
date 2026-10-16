from __future__ import annotations

from pathlib import Path

import pytest

from wuwa_assistant.public_data.redeem_models import COMMUNITY_URL, RedeemCode
from wuwa_assistant.public_data.redeem_service import RedeemCodeService
from wuwa_assistant.public_data.redeem_sources import parse_community, parse_official

COMMUNITY = """
<script>Here are all the active WuWa codes: EVILCODE - fake How do I redeem</script>
<p><strong>Here are all the active WuWa codes:</strong></p>
<ul><li><strong>TESTGIFT</strong> - rewards</li><li>TESTGIFT - duplicate</li></ul>
<h2>How do I redeem Wuthering Waves codes?</h2>
<p>UNRELATEDCODE - not a gift</p>
<p>Expired codes:</p><p>OLDGIFT | OLDERGIFT</p>
"""


def test_community_only_reads_active_and_expired_sections() -> None:
    parsed = parse_community(COMMUNITY)
    assert [(c.code, c.status) for c in parsed] == [
        ("TESTGIFT", "listed"),
        ("OLDGIFT", "expired"),
        ("OLDERGIFT", "expired"),
    ]
    with pytest.raises(ValueError):
        parse_community("<html>TESTGIFT - rewards</html>")


def test_official_requires_explicit_label_and_ignores_scripts_images_dates() -> None:
    html = (
        "<p>兌換碼: <b>TESTGIFT</b></p><p>序號: ANOTHER123</p>"
        '<p>兌換碼: 20261012</p><img src="https://example.org/IMAGECODE">'
        "<script>兌換碼: SCRIPTCODE</script><p>UNLABELLEDCODE</p>"
    )
    assert [c.code for c in parse_official(html, 123)] == ["TESTGIFT", "ANOTHER123"]


@pytest.mark.parametrize("code", ["bad code", "123456", "<script>", "A" * 41])
def test_code_validation(code: str) -> None:
    with pytest.raises(ValueError):
        RedeemCode(code, COMMUNITY_URL)


def test_cache_partial_failures_removal_expiration_and_dedup(tmp_path: Path) -> None:
    def fail() -> tuple[RedeemCode, ...]:
        raise OSError("offline")

    service = RedeemCodeService(
        tmp_path, official=lambda: (), community=lambda: parse_community(COMMUNITY)
    )
    first = service.synchronize()
    assert first.fresh and len(first.codes) == 3
    assert service.load_cached().codes == first.codes
    assert not service.load_cached().fresh
    failing = RedeemCodeService(tmp_path, official=fail, community=fail)
    stale = failing.synchronize()
    assert not stale.fresh and stale.codes == first.codes
    assert stale.checked_at == first.checked_at
    partial = RedeemCodeService(
        tmp_path,
        official=lambda: (
            RedeemCode("OLDGIFT", "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/1"),
        ),
        community=fail,
    ).synchronize()
    assert next(c for c in partial.codes if c.code == "OLDGIFT").status == "expired"
    assert "TESTGIFT" not in partial.checked_codes
    changed = RedeemCodeService(
        tmp_path,
        official=lambda: (),
        community=lambda: (RedeemCode("NEWGIFT", COMMUNITY_URL, "listed"),),
    ).synchronize()
    assert next(c for c in changed.codes if c.code == "TESTGIFT").status == "unlisted"
    assert next(c for c in changed.codes if c.code == "NEWGIFT").available


def test_corrupt_cache_and_untrusted_links_are_rejected(tmp_path: Path) -> None:
    service = RedeemCodeService(tmp_path)
    service.path.write_text(
        '{"schema_version":1,"codes":[{"code":"TESTGIFT","source_url":"https://evil.test"}],"checked_at":"2026-10-12T00:00:00+00:00"}'
    )
    assert service.load_cached().codes == ()
    with pytest.raises(ValueError):
        RedeemCode("TESTGIFT", "https://www.pockettactics.com@evil.test/wuthering-waves/codes")
