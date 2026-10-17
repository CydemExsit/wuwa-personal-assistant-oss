"""Create the portable alternative and checksums beside the verified installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", required=True, type=Path)
    output = parser.parse_args().output_directory.resolve()
    root = Path(__file__).resolve().parents[1]
    if not output.is_relative_to(root / "dist"):
        raise ValueError("Release output must stay within dist")
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    edition = json.loads((root / "packaging/distribution.json").read_text(encoding="utf-8"))
    package = output / "WuwaDisplayAssistant"
    installer = output / f"{edition['asset']}-{version}-Setup.exe"
    if not installer.is_file():
        raise FileNotFoundError("Verified installer missing")
    for report in (
        "package-audit.json",
        "package-validation.json",
        "installer-validation.json",
        "update-validation.json",
    ):
        payload = json.loads((output / "reports" / report).read_text(encoding="utf-8-sig"))
        if payload.get("errors") or ("status" in payload and payload["status"] != "passed"):
            raise ValueError(f"Verification failed: {report}")
    portable = output / f"{edition['asset']}-{version}-windows-x64.zip"
    with zipfile.ZipFile(portable, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output))
    with zipfile.ZipFile(portable) as archive:
        if archive.testzip() is not None:
            raise ValueError("Portable ZIP failed integrity verification")
    with zipfile.ZipFile(output / f"VerificationReports-{version}.zip", "w") as archive:
        for path in sorted((output / "reports").glob("*.json")):
            archive.write(path, path.name)
    assets = [
        installer,
        portable,
        *sorted(output.glob("ThirdPartySources-*.zip")),
        *sorted(output.glob("certifi-*.tar.gz")),
        output / f"VerificationReports-{version}.zip",
    ]
    lines = []
    for path in assets:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    (output / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")
    print(f"Installer, portable ZIP and verified checksums ready ({len(assets)} assets).")


if __name__ == "__main__":
    main()
