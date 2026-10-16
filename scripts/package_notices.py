"""Collect original build-environment licenses without exporting local metadata."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
from pathlib import Path, PurePosixPath


def collect_notices(root: Path) -> list[tuple[str, str]]:
    output = root / "build/package-legal"
    output.mkdir(parents=True, exist_ok=True)
    source_licenses = root / "build/third-party-sources/licenses"
    source_manifest = root / "build/third-party-sources/SOURCE_MANIFEST.json"
    if not source_licenses.is_dir() or not source_manifest.is_file():
        raise RuntimeError("Prepare corresponding upstream sources before packaging")
    files: dict[str, str] = {}
    data: list[tuple[str, str]] = []

    def add(source: Path, relative: str) -> None:
        target = output / relative
        if not target.resolve().is_relative_to(output.resolve()):
            raise RuntimeError("Invalid license destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        data.append((str(target), str(PurePosixPath("legal") / PurePosixPath(relative).parent)))

    for source in sorted(source_licenses.rglob("*")):
        if source.is_file():
            add(source, "upstream/" + source.relative_to(source_licenses).as_posix())
    add(source_manifest, "SOURCE_MANIFEST.json")
    add(root / "docs/dependency-licensing.md", "DEPENDENCY-LICENSING.md")
    add(root / "third_party/PaddleOCR-MODEL-NOTICE.txt", "PaddleOCR-MODEL-NOTICE.txt")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime license is unavailable")
    add(python_license, "python/LICENSE.txt")
    dependencies = []
    for distribution in sorted(
        importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()
    ):
        name = distribution.metadata["Name"]
        if name.lower().replace("_", "-").startswith("wuwa-personal-assistant"):
            continue
        directory = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{name}-{distribution.version}")
        count = 0
        for item in distribution.files or []:
            relative = PurePosixPath(str(item).replace("\\", "/"))
            if ".." in relative.parts or "commercial" in relative.name.lower():
                continue
            if not relative.name.upper().startswith(
                ("LICENSE", "COPYING", "NOTICE", "COPYRIGHT", "THIRDPARTYNOTICES")
            ):
                continue
            source = Path(distribution.locate_file(item))
            if source.is_file():
                add(source, f"dependencies/{directory}/{relative.as_posix()}")
                count += 1
        dependencies.append(
            {
                "name": name,
                "version": distribution.version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "license_files": count,
            }
        )
    manifest = {
        "scope": "build-environment license inventory; not every listed package is bundled",
        "dependencies": dependencies,
        "files": files,
    }
    manifest_file = output / "LICENSE_MANIFEST.json"
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    data.append((str(manifest_file), "legal"))
    return data
