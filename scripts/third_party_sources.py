"""Download immutable upstream library sources for release redistribution.

Only build-time network access is used. Full archives and upstream license files
are preserved; archive contents are never executed. Cache hashes are verified.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen


def verify_runtime_versions(expected: dict[str, str]) -> None:
    import shapely
    from PySide6.QtCore import qVersion

    actual = {
        "PySide6": importlib.metadata.version("PySide6"),
        "Qt": qVersion(),
        "shapely": shapely.__version__,
        "GEOS": shapely.geos_version_string,
    }
    if actual != expected:
        raise RuntimeError(f"Corresponding sources do not match build runtime: {actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "third_party/upstream-sources.json").read_text(encoding="utf-8"))
    verify_runtime_versions(payload["runtime_versions"])
    cache = root / "build/third-party-sources"
    cache.mkdir(parents=True, exist_ok=True)
    licenses = cache / "licenses"
    records = []
    archives = []
    for component in payload["components"]:
        repository, commit = component["repository"], component["commit"]
        url = f"https://codeload.github.com/{repository}/tar.gz/{commit}"
        name = repository.split("/")[-1]
        archive = cache / f"{name}-{commit}.tar.gz"
        if not archive.is_file():
            temporary = archive.with_suffix(".download")
            request = Request(url, headers={"User-Agent": "wuwa-assistant-source-build"})
            with urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            temporary.replace(archive)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        expected = component.get("sha256")
        if expected and digest != expected:
            raise SystemExit(f"Upstream source checksum mismatch: {name}")
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                relative = PurePosixPath(member.name)
                if not member.isfile() or ".." in relative.parts:
                    continue
                if not (
                    relative.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT"))
                    or "LICENSES" in relative.parts
                ):
                    continue
                target = licenses / name / Path(*relative.parts[1:])
                if not target.resolve().is_relative_to(licenses.resolve()):
                    raise SystemExit("Invalid upstream license path")
                target.parent.mkdir(parents=True, exist_ok=True)
                content = source.extractfile(member)
                assert content is not None
                target.write_bytes(content.read())
        records.append({**component, "url": url, "sha256": digest, "archive": archive.name})
        archives.append(archive)
        print(f"Verified upstream source: {repository} ({commit[:12]})", flush=True)
    manifest = {"runtime_versions": payload["runtime_versions"], "components": records}
    (cache / "SOURCE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for sdist in payload.get("sdists", []):
        archive = cache / sdist["filename"]
        if not archive.is_file():
            with urlopen(sdist["url"], timeout=120) as response, archive.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != sdist["sha256"]:
            raise SystemExit("Upstream sdist checksum mismatch")
        shutil.copy2(archive, args.output_directory / archive.name)
    destination = args.output_directory / payload["source_bundle"]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as bundle:
        for archive in archives:
            bundle.write(archive, archive.name)
        bundle.write(cache / "SOURCE_MANIFEST.json", "SOURCE_MANIFEST.json")
    print(f"Corresponding-source bundle ready: {destination.name}")


if __name__ == "__main__":
    main()
