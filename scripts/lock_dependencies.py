"""Hash a tested Windows/Python3.12 environment using public PyPI release metadata.

Run only after installing the desired dev+packaging environment and pip check.
No installed paths, editable URLs, credentials or machine identity are written.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen


def release_line(item: tuple[str, str]) -> str:
    name, version = item
    url = f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json"
    with urlopen(url, timeout=60) as response:
        payload = json.load(response)
    hashes = sorted({entry["digests"]["sha256"] for entry in payload["urls"]})
    if not hashes:
        raise ValueError(f"No published release hashes: {name}=={version}")
    return f"{name}=={version} \\\n" + " \\\n".join(
        f"    --hash=sha256:{value}" for value in hashes
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=Path("requirements/windows-py312.lock"))
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Generate this lock with Python3.12")
    if args.snapshot:
        rows = [line.split("==", 1) for line in args.snapshot.read_text().splitlines() if line]
        packages = {name: version for name, version in rows}
    else:
        packages = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    packages = {
        name: version
        for name, version in packages.items()
        if name.lower().replace("_", "-")
        not in {
            "pip",
            "wuwa-personal-assistant",
            "wuwa-personal-assistant-display",
            "wuwa-personal-assistant-oss",
        }
    }
    items = sorted(packages.items(), key=lambda item: item[0].lower())
    with ThreadPoolExecutor(max_workers=8) as pool:
        lines = list(pool.map(release_line, items))
    header = (
        "# Tested Windows x64 / CPython3.12 environment.\n"
        "# SHA-256 values are from public PyPI release metadata.\n"
        "# Regenerate with scripts/lock_dependencies.py after pip check.\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(header + "\n\n".join(lines) + "\n", encoding="utf-8")
    print(f"Locked {len(items)} third-party distributions; no local paths recorded.")


if __name__ == "__main__":
    main()
