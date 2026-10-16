"""Reject private/generated artifacts and personal home paths in publishable files.

Use with a separate redacted full-history secret scan; this is a prevention
check, not a claim that automated scanning can prove the absence of all secrets.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {
    "samples_private",
    "local_data",
    "debug_output",
    ".codex",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "work",
    "secrets",
    "credentials",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".exe",
    ".dll",
    ".pyd",
    ".zip",
    ".dmp",
    ".bak",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".whl",
}
HOME_PATH = re.compile(
    r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"']+"
    r"|/(?:home|Users)/[^\s\"']+",
    re.IGNORECASE,
)


def inspect_file(relative: str, content: bytes) -> list[str]:
    path = PurePosixPath(relative.replace("\\", "/"))
    errors = []
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        errors.append("private/generated directory")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.startswith(".env"):
        errors.append("private/generated file type")
    if (
        path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        and relative != "docs/images/echo-panel-demo.png"
    ):
        errors.append("unreviewed screenshot/artwork")
    if len(content) > 5 * 1024 * 1024:
        errors.append("oversized source artifact")
    if content.startswith(b"SQLite format 3\0"):
        errors.append("database content")
    if b"\0" not in content[:8192]:
        text = content.decode("utf-8", "replace")
        if HOME_PATH.search(text):
            errors.append("personal home path")
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text):
            errors.append("private key material")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
    )
    paths = sorted({name.decode("utf-8") for name in raw.split(b"\0") if name})
    errors = []
    for name in paths:
        path = root / name
        if path.is_symlink():
            errors.append(f"{name}: symlink requires manual review")
        elif path.is_file():
            errors.extend(f"{name}: {reason}" for reason in inspect_file(name, path.read_bytes()))
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Public-file guard passed: {len(paths)} files; values are never printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
