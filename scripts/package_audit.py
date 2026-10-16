"""Verify a Windows package against its source and the build's binary inventory.

Reports intentionally contain only version numbers, relative paths and hashes;
local installation paths and editable dependency URLs are never exported.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import CodeType
from typing import Any

from wuwa_assistant.packaging_policy import excluded_binary

REQUIRED_MODULES = {
    "wuwa_assistant",
    "wuwa_assistant.app.main",
    "wuwa_assistant.application.echo_history",
    "wuwa_assistant.application.equipment_overview",
    "wuwa_assistant.application.characters",
    "wuwa_assistant.ui.main_window",
    "wuwa_assistant.ui.controllers.echo_workspace",
    "wuwa_assistant.ocr.rapidocr_engine",
    "wuwa_assistant.reminders.background",
}
REQUIRED_RUNTIME_FILES = (
    "rapidocr/models/PP-OCRv6_det_small.onnx",
    "rapidocr/models/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "rapidocr/models/PP-OCRv6_rec_small.onnx",
    "PySide6/plugins/platforms/qwindows.dll",
    "tzdata/zoneinfo/Asia/Taipei",
)
RESOURCE_SUFFIXES = {".json", ".png", ".qss", ".svg", ".ico", ".mako"}
MODEL_HASHES = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": (
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"
    ),
}


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _normalized(code: CodeType) -> CodeType:
    return code.replace(
        co_filename="",
        co_consts=tuple(
            _normalized(value) if isinstance(value, CodeType) else value for value in code.co_consts
        ),
    )


def audit_modules(
    source_root: Path, modules: Mapping[str, CodeType], entry_point: CodeType
) -> tuple[dict[str, str], list[str]]:
    """Compare compiled code, including nested functions, independent of build paths."""

    errors = [
        f"Missing application module: {name}" for name in sorted(REQUIRED_MODULES - modules.keys())
    ]
    hashes: dict[str, str] = {}
    for name, bundled in sorted(modules.items()):
        source = source_root.joinpath(*name.split("."))
        source = source / "__init__.py" if source.is_dir() else source.with_suffix(".py")
        if not source.is_file():
            errors.append(f"Unexpected application module: {name}")
            continue
        compiled = compile(source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=1)
        if _normalized(bundled) != _normalized(compiled):
            errors.append(f"Application module differs from source: {name}")
        hashes[name] = _sha256(source)
    entry_source = source_root / "wuwa_assistant/app/__main__.py"
    compiled_entry = compile(
        entry_source.read_bytes(), str(entry_source), "exec", dont_inherit=True, optimize=1
    )
    if _normalized(entry_point) != _normalized(compiled_entry):
        errors.append("Application entry point differs from source")
    hashes["wuwa_assistant.app.__main__"] = _sha256(entry_source)
    return hashes, errors


def audit_resources(source_root: Path, internal_root: Path) -> tuple[dict[str, str], list[str]]:
    hashes: dict[str, str] = {}
    errors: list[str] = []
    for source in sorted((source_root / "wuwa_assistant").rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        migration = relative.is_relative_to("wuwa_assistant/storage/migrations")
        if source.suffix not in RESOURCE_SUFFIXES and not (migration and source.suffix == ".py"):
            continue
        packaged = internal_root / relative
        hashes[relative.as_posix()] = _sha256(source)
        if not packaged.is_file():
            errors.append(f"Missing resource: {relative.as_posix()}")
        elif _sha256(packaged) != hashes[relative.as_posix()]:
            errors.append(f"Resource differs from source: {relative.as_posix()}")
    for relative_name in REQUIRED_RUNTIME_FILES:
        if not (internal_root / relative_name).is_file():
            errors.append(f"Missing runtime resource: {relative_name}")
    return hashes, errors


def binary_inventory(analysis: object) -> dict[str, Path]:
    """Read binary records without executing PyInstaller's generated TOC file."""

    result: dict[str, Path] = {}
    if not isinstance(analysis, (tuple, list)):
        return result
    if (
        len(analysis) == 3
        and all(isinstance(value, str) for value in analysis)
        and analysis[2] in {"BINARY", "EXTENSION"}
    ):
        if not excluded_binary(analysis[0]):
            result[Path(analysis[0]).as_posix()] = Path(analysis[1])
    else:
        for child in analysis:
            result.update(binary_inventory(child))
    return result


def audit_binaries(
    internal_root: Path, inventory: Mapping[str, Path], allowed_roots: tuple[Path, ...]
) -> tuple[dict[str, str], list[str]]:
    hashes: dict[str, str] = {}
    errors: list[str] = []
    allowed_roots = tuple(root.resolve() for root in allowed_roots)
    if not inventory:
        errors.append("Build binary inventory is empty")
    for relative, source in sorted(inventory.items()):
        packaged = internal_root / relative
        if not any(source.resolve().is_relative_to(root) for root in allowed_roots):
            errors.append(f"Binary comes from outside the build runtime: {relative}")
        if not packaged.is_file():
            errors.append(f"Missing binary: {relative}")
        elif not source.is_file() or _sha256(packaged) != _sha256(source):
            errors.append(f"Binary differs from build input: {relative}")
        else:
            hashes[relative] = _sha256(packaged)
    for packaged in sorted(internal_root.rglob("*")):
        if packaged.suffix.lower() not in {".dll", ".pyd"}:
            continue
        relative = packaged.relative_to(internal_root).as_posix()
        if excluded_binary(relative):
            errors.append(f"Unsupported optional binary: {relative}")
        if relative not in inventory:
            errors.append(f"Unexpected binary absent from build inventory: {relative}")
        # Qt's verified Windows wheel does not use external ICU DLLs. This guard
        # also catches the unversioned ICU payload previously picked up from PATH.
        if packaged.name.lower().startswith(("icuuc", "icuin", "icudt")):
            errors.append(f"Unexpected ICU binary: {relative}")
    return hashes, errors


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def audit_legal_files(internal_root: Path) -> tuple[dict[str, str], list[str]]:
    legal = internal_root / "legal"
    errors: list[str] = []
    hashes: dict[str, str] = {}
    for name in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "LICENSE_MANIFEST.json",
        "SOURCE_MANIFEST.json",
        "DEPENDENCY-LICENSING.md",
        "PaddleOCR-MODEL-NOTICE.txt",
    ):
        path = legal / name
        if not path.is_file():
            errors.append(f"Missing legal file: {name}")
        else:
            hashes[name] = _sha256(path)
    manifest_file = legal / "LICENSE_MANIFEST.json"
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        for relative, digest in manifest["files"].items():
            path = (legal / relative).resolve()
            if not path.is_relative_to(legal.resolve()):
                errors.append("Invalid legal manifest path")
            elif not path.is_file() or _sha256(path) != digest:
                errors.append(f"License differs from manifest: {relative}")
            else:
                hashes[relative] = digest
    return hashes, errors


def audit_model_hashes(internal_root: Path) -> tuple[dict[str, str], list[str]]:
    hashes = {}
    errors = []
    for name, expected in MODEL_HASHES.items():
        path = internal_root / "rapidocr/models" / name
        if not path.is_file() or _sha256(path) != expected:
            errors.append(f"OCR model differs from upstream provenance: {name}")
        else:
            hashes[name] = expected
    return hashes, errors


def build_environment() -> dict[str, object]:
    return {
        "python": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "dependencies": sorted(
            (
                {"name": distribution.metadata["Name"], "version": distribution.version}
                for distribution in importlib.metadata.distributions()
            ),
            key=lambda item: item["name"].lower(),
        ),
    }


def audit_package(repository_root: Path, package_root: Path, analysis_toc: Path) -> dict[str, Any]:
    # Packaging remains an optional dependency for ordinary application/test use.
    from PyInstaller.archive.readers import CArchiveReader

    executable = package_root / "WuwaDisplayAssistant.exe"
    archive = CArchiveReader(str(executable))
    pyz_name = next(name for name in archive.toc if name.endswith(".pyz"))
    pyz = archive.open_embedded_archive(pyz_name)
    modules = {
        name: pyz.extract(name)
        for name in pyz.toc
        if name == "wuwa_assistant" or name.startswith("wuwa_assistant.")
    }
    import marshal

    entry_point = marshal.loads(archive.extract("__main__"))
    module_hashes, module_errors = audit_modules(repository_root / "src", modules, entry_point)
    internal_root = package_root / "_internal"
    resources, resource_errors = audit_resources(repository_root / "src", internal_root)
    inventory = binary_inventory(ast.literal_eval(analysis_toc.read_text(encoding="utf-8")))
    allowed_roots = (Path(sys.prefix), Path(sys.base_prefix), Path(os.environ["SYSTEMROOT"]))
    binaries, binary_errors = audit_binaries(internal_root, inventory, allowed_roots)
    legal, legal_errors = audit_legal_files(internal_root)
    models, model_errors = audit_model_hashes(internal_root)
    project = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    errors = module_errors + resource_errors + binary_errors + legal_errors + model_errors
    if version not in modules["wuwa_assistant"].co_consts:
        errors.append("Packaged application version differs from pyproject.toml")
    return {
        "status": "failed" if errors else "passed",
        "version": version,
        "executable_sha256": _sha256(executable),
        "application_modules": module_hashes,
        "resources": resources,
        "binaries": binaries,
        "legal_files": legal,
        "ocr_models": models,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report-directory", type=Path, required=True)
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--analysis-toc", type=Path)
    args = parser.parse_args()
    args.report_directory.mkdir(parents=True, exist_ok=True)
    _write_json(args.report_directory / "build-environment.json", build_environment())
    if args.package_root is None:
        return 0
    if args.analysis_toc is None:
        parser.error("--analysis-toc is required when auditing a package")
    report = audit_package(args.repository_root, args.package_root, args.analysis_toc)
    _write_json(args.report_directory / "package-audit.json", report)
    if report["errors"]:
        print("\n".join(report["errors"]), file=sys.stderr)
        return 1
    print(
        f"Package audit passed: {len(report['application_modules'])} application modules, "
        f"{len(report['resources'])} resources, {len(report['binaries'])} binaries; "
        f"version {report['version']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
