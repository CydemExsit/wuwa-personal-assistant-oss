"""Explicit software updates, separate from public game data and user storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from wuwa_assistant import __version__
from wuwa_assistant.distribution import ASSET_PREFIX, EDITION_ID, UPDATE_REPOSITORY

MAX_INSTALLER_BYTES = 512 * 1024 * 1024
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class UpdateError(Exception):
    """A message safe to show without local paths, URLs or credentials."""


def version_tuple(version: str) -> tuple[int, ...]:
    if _VERSION.fullmatch(version) is None:
        raise UpdateError("版本格式不正確, 請使用正式版安裝檔。")
    return tuple(int(part) for part in version.split("."))


def _trusted_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in _HOSTS
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


class _ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> Request | None:
        if not _trusted_url(newurl):
            raise UpdateError("更新下載被導向非預期的網站, 已停止。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PreparedUpdate:
    path: Path
    sha256: str
    version: str


def parse_release(
    payload: object, repository: str, *, current: str = __version__
) -> Release | None:
    if (
        not isinstance(payload, dict)
        or payload.get("draft") is not False
        or payload.get("prerelease") is not False
    ):
        raise UpdateError("目前沒有可用的正式更新。")
    tag = payload.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("正式更新版本資訊不完整。")
    version = tag[1:]
    if version_tuple(version) <= version_tuple(current):
        return None
    name = f"{ASSET_PREFIX}-{version}-Setup.exe"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("新版安裝檔尚未發布, 請稍後再試。")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        raise UpdateError("找不到這個版本的安裝檔, 請稍後再試。")
    asset = matches[0]
    digest, size = asset.get("digest"), asset.get("size")
    expected = f"https://github.com/{repository}/releases/download/{tag}/{name}"
    if (
        asset.get("browser_download_url") != expected
        or asset.get("state") != "uploaded"
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or type(size) is not int
        or not 0 < size <= MAX_INSTALLER_BYTES
    ):
        raise UpdateError("更新檔的來源或完整性資訊不正確, 已停止。")
    return Release(version, expected, digest[7:], size)


class UpdateClient:
    def __init__(self, cache: Path) -> None:
        self.cache = cache / EDITION_ID
        self._opener = build_opener(_ReleaseRedirects())

    def check(self, cancel: Event) -> Release | None:
        if UPDATE_REPOSITORY is None:
            raise UpdateError("Display 維持私人發布, 請使用收到的新版安裝檔。")
        try:
            request = Request(
                f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "WuwaAssistant-Updater",
                },
            )
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
            if cancel.is_set():
                raise UpdateError("已取消更新。")
            if len(raw) > 1024 * 1024:
                raise UpdateError("更新資訊超過預期大小, 已停止。")
            return parse_release(json.loads(raw), UPDATE_REPOSITORY)
        except HTTPError as exc:
            raise UpdateError("暫時無法檢查更新, 請稍後重試; 原版本仍可正常使用。") from exc
        except (URLError, OSError, ValueError) as exc:
            raise UpdateError("無法連線取得更新, 請確認網路後重試。") from exc

    def download(
        self, release: Release, cancel: Event, progress: Callable[[int], None]
    ) -> PreparedUpdate:
        # Validate again at the network boundary; callers cannot supply another host/repository.
        expected = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/v{release.version}/{ASSET_PREFIX}-{release.version}-Setup.exe"
        if (
            UPDATE_REPOSITORY is None
            or release.url != expected
            or not 0 < release.size <= MAX_INSTALLER_BYTES
        ):
            raise UpdateError("更新來源不正確, 已停止。")
        self.cache.mkdir(parents=True, exist_ok=True)
        target = self.cache / f"{ASSET_PREFIX}-{release.version}-Setup.exe"
        part = self.cache / f"{uuid4().hex}.part"
        digest, count, deadline = hashlib.sha256(), 0, time.monotonic() + 600
        try:
            request = Request(release.url, headers={"User-Agent": "WuwaAssistant-Updater"})
            with self._opener.open(request, timeout=10) as response, part.open("xb") as output:
                while True:
                    if cancel.is_set():
                        raise UpdateError("已取消下載, 原版本仍可使用。")
                    if time.monotonic() > deadline:
                        raise UpdateError("下載時間過長, 請稍後重試。")
                    block = response.read(256 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > release.size:
                        raise UpdateError("更新檔大小不符, 已停止。")
                    output.write(block)
                    digest.update(block)
                    progress(min(99, int(count * 100 / release.size)))
            if count != release.size or digest.hexdigest() != release.sha256:
                raise UpdateError("更新檔驗證失敗, 請重新下載; 尚未變更程式。")
            with part.open("rb") as header:
                is_executable = header.read(2) == b"MZ"
            if not is_executable:
                raise UpdateError("下載的檔案不是 Windows 安裝程式。")
            part.replace(target)
            progress(100)
            return PreparedUpdate(target, release.sha256, release.version)
        except (URLError, OSError) as exc:
            raise UpdateError("下載未完成, 請檢查網路與磁碟空間後重試。") from exc
        finally:
            part.unlink(missing_ok=True)

    def import_installer(self, source: Path, cancel: Event) -> PreparedUpdate:
        pattern = re.escape(ASSET_PREFIX) + r"-(.+)-Setup\.exe"
        match = re.fullmatch(pattern, source.name)
        if match is None or version_tuple(match[1]) <= version_tuple(__version__):
            raise UpdateError("請選擇同一版本系列、版本號較新的正式 Setup.exe 安裝檔。")
        self.cache.mkdir(parents=True, exist_ok=True)
        part = self.cache / f"{uuid4().hex}.part"
        target = self.cache / source.name
        digest, count = hashlib.sha256(), 0
        try:
            with source.open("rb") as incoming, part.open("xb") as output:
                if incoming.read(2) != b"MZ":
                    raise UpdateError("這個檔案不是 Windows 安裝程式。")
                incoming.seek(0)
                while block := incoming.read(256 * 1024):
                    if cancel.is_set():
                        raise UpdateError("已取消更新。")
                    count += len(block)
                    if count > MAX_INSTALLER_BYTES:
                        raise UpdateError("安裝檔超過預期大小, 已停止。")
                    digest.update(block)
                    output.write(block)
            part.replace(target)
            return PreparedUpdate(target, digest.hexdigest(), match[1])
        except OSError as exc:
            raise UpdateError("無法讀取安裝檔, 請重新選擇。") from exc
        finally:
            part.unlink(missing_ok=True)


def launch_installer(update: PreparedUpdate, data_root: Path) -> None:
    """Called only after workers and database are closed; Setup waits for our exit."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        raise UpdateError("請在 Windows 安裝版使用更新, 開發環境請自行更新原始碼。")
    with update.path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != update.sha256:
            raise UpdateError("安裝檔已變更, 更新已停止, 請重新選擇或下載。")
    current = Path(sys.executable).resolve().parent
    target = (
        current
        if (current / "unins000.exe").is_file()
        else Path(os.environ["LOCALAPPDATA"]) / "Programs" / EDITION_ID
    )
    subprocess.Popen(
        [
            str(update.path),
            "/SILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/WUWAUPDATE=1",
            f"/WUWAUPDATEPID={os.getpid()}",
            f"/WUWADATA={data_root}",
            f"/DIR={target}",
        ],
        close_fds=True,
    )
