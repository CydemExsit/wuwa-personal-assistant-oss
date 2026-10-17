from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import wuwa_assistant.updates as updates
from wuwa_assistant.distribution import ASSET_PREFIX
from wuwa_assistant.updates import PreparedUpdate, UpdateClient, UpdateError, parse_release

REPO = "owner/project"
DATA = b"MZ" + b"example-installer" * 100
HASH = hashlib.sha256(DATA).hexdigest()


def payload() -> dict[str, Any]:
    name = f"{ASSET_PREFIX}-9.0.0-Setup.exe"
    return {
        "draft": False,
        "prerelease": False,
        "tag_name": "v9.0.0",
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://github.com/{REPO}/releases/download/v9.0.0/{name}",
                "digest": f"sha256:{HASH}",
                "size": len(DATA),
                "state": "uploaded",
            }
        ],
    }


class Opener:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests: list[Any] = []

    def open(self, request: Any, **_kwargs: object) -> io.BytesIO:
        self.requests.append(request)
        return io.BytesIO(self.data)


def test_stable_release_requires_newer_version_and_correct_edition() -> None:
    assert parse_release(payload(), REPO, current="8.9.9") is not None
    assert parse_release(payload(), REPO, current="9.0.0") is None
    assert parse_release(payload(), REPO, current="10.0.0") is None
    data = payload()
    data["assets"][0]["name"] = "OtherEdition-9.0.0-Setup.exe"
    with pytest.raises(UpdateError):
        parse_release(data, REPO)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("browser_download_url", "https://evil.example/installer.exe"),
        ("browser_download_url", "http://github.com/owner/project/file.exe"),
        ("digest", None),
        ("digest", "sha256:garbage"),
        ("size", True),
        ("size", 0),
        ("size", updates.MAX_INSTALLER_BYTES + 1),
        ("state", "new"),
    ],
)
def test_untrusted_or_incomplete_assets_rejected(field: str, value: object) -> None:
    data = payload()
    data["assets"][0][field] = value
    with pytest.raises(UpdateError):
        parse_release(data, REPO)


@pytest.mark.parametrize("tag", ["v9.0.0-beta.1", "v9.0", "v9.00.0", "v9999999.0.0", "../9.0.0"])
def test_nonstable_and_path_versions_rejected(tag: str) -> None:
    data = payload()
    data["tag_name"] = tag
    with pytest.raises(UpdateError):
        parse_release(data, REPO)


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_draft_and_prerelease_never_offered(field: str) -> None:
    data = payload()
    data[field] = True
    with pytest.raises(UpdateError):
        parse_release(data, REPO)


def test_anonymous_check_uses_only_release_api(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(updates, "UPDATE_REPOSITORY", REPO)
    client = UpdateClient(tmp_path)
    opener = Opener(json.dumps(payload()).encode())
    client._opener = opener  # type: ignore[assignment]
    assert client.check(Event()) is not None
    assert opener.requests[0].full_url == f"https://api.github.com/repos/{REPO}/releases/latest"
    assert not opener.requests[0].has_header("Authorization")
    assert not tmp_path.joinpath(updates.EDITION_ID).exists()


def test_private_channel_never_requests_credentials_or_network(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(updates, "UPDATE_REPOSITORY", None)
    client = UpdateClient(tmp_path)
    opener = Opener(b"must not be read")
    client._opener = opener  # type: ignore[assignment]
    with pytest.raises(UpdateError, match="私人"):
        client.check(Event())
    assert not opener.requests


@pytest.mark.parametrize("body", [DATA, DATA[:-1], DATA + b"extra", b"MZ" + b"corrupt"])
def test_download_atomic_integrity_and_cleanup(
    body: bytes, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(updates, "UPDATE_REPOSITORY", REPO)
    client = UpdateClient(tmp_path)
    client._opener = Opener(body)  # type: ignore[assignment]
    release = parse_release(payload(), REPO)
    assert release is not None
    if body == DATA:
        progress: list[int] = []
        prepared = client.download(release, Event(), progress.append)
        assert prepared.path.read_bytes() == DATA
        assert progress[-1] == 100
    else:
        with pytest.raises(UpdateError):
            client.download(release, Event(), lambda _: None)
        assert not list(client.cache.glob("*.exe"))
    assert not list(client.cache.glob("*.part"))


def test_cancelled_download_does_not_stage_executable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(updates, "UPDATE_REPOSITORY", REPO)
    client = UpdateClient(tmp_path)
    client._opener = Opener(DATA)  # type: ignore[assignment]
    event = Event()
    event.set()
    release = parse_release(payload(), REPO)
    assert release is not None
    with pytest.raises(UpdateError, match="取消"):
        client.download(release, event, lambda _: None)
    assert not list(client.cache.glob("*"))


def test_local_installer_is_copied_and_other_series_rejected(tmp_path: Path) -> None:
    source = tmp_path / f"{ASSET_PREFIX}-9.0.0-Setup.exe"
    source.write_bytes(DATA)
    client = UpdateClient(tmp_path / "cache")
    result = client.import_installer(source, Event())
    assert result.path != source
    assert result.path.read_bytes() == source.read_bytes()
    assert result.sha256 == HASH
    other = tmp_path / "Other-9.0.0-Setup.exe"
    other.write_bytes(DATA)
    with pytest.raises(UpdateError):
        client.import_installer(other, Event())


def test_launch_rechecks_hash_and_passes_unicode_paths_without_shell(
    tmp_path: Path, monkeypatch: Any
) -> None:
    installer = tmp_path / "更新 安裝.exe"
    installer.write_bytes(DATA)
    current = tmp_path / "已安裝"
    current.mkdir()
    (current / "unins000.exe").touch()
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.sys, "executable", str(current / "app.exe"))
    calls: list[Any] = []
    monkeypatch.setattr(
        updates.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    prepared = PreparedUpdate(installer, HASH, "9.0.0")
    updates.launch_installer(prepared, tmp_path / "保留 資料")
    args = calls[0][0][0]
    assert f"/DIR={current}" in args
    assert f"/WUWADATA={tmp_path / '保留 資料'}" in args
    assert f"/WUWAUPDATEPID={updates.os.getpid()}" in args
    assert "shell" not in calls[0][1]
    installer.write_bytes(b"MZchanged")
    with pytest.raises(UpdateError, match="變更"):
        updates.launch_installer(prepared, tmp_path)
    assert len(calls) == 1


def test_packaging_and_runtime_identities_agree() -> None:
    from wuwa_assistant.distribution import EDITION_ID, EDITION_NAME

    data = json.loads(
        (Path(__file__).parents[1] / "packaging/distribution.json").read_text("utf-8")
    )
    assert (data["id"], data["name"], data["asset"]) == (EDITION_ID, EDITION_NAME, ASSET_PREFIX)
