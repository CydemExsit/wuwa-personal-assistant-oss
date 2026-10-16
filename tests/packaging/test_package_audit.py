from __future__ import annotations

import importlib.util
from pathlib import Path
from types import CodeType
from typing import ClassVar

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "package_audit", Path(__file__).resolve().parents[2] / "scripts/package_audit.py"
)
assert _SPEC is not None and _SPEC.loader is not None
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _write(root: Path, relative: str, content: bytes = b"verified input") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _application(source_root: Path) -> tuple[dict[str, CodeType], CodeType]:
    modules = {}
    for name in audit.REQUIRED_MODULES:
        relative = name.replace(".", "/")
        relative += "/__init__.py" if name == "wuwa_assistant" else ".py"
        path = _write(source_root, relative, b"def nested():\n    return 'current'\n")
        modules[name] = compile(
            path.read_bytes(), "different/build/path.py", "exec", dont_inherit=True, optimize=1
        )
    entry = _write(source_root, "wuwa_assistant/app/__main__.py", b"print('entry')\n")
    return modules, compile(entry.read_bytes(), "entry", "exec", dont_inherit=True, optimize=1)


def test_module_audit_ignores_build_paths_but_detects_stale_nested_code(tmp_path: Path) -> None:
    modules, entry = _application(tmp_path)
    hashes, errors = audit.audit_modules(tmp_path, modules, entry)
    assert not errors
    assert len(hashes) == len(audit.REQUIRED_MODULES) + 1

    source = tmp_path / "wuwa_assistant/app/main.py"
    source.write_text("def nested():\n    return 'changed'\n", encoding="utf-8")
    _, errors = audit.audit_modules(tmp_path, modules, entry)
    assert errors == ["Application module differs from source: wuwa_assistant.app.main"]


def test_module_audit_rejects_missing_required_module_and_stale_entry(tmp_path: Path) -> None:
    modules, _ = _application(tmp_path)
    del modules["wuwa_assistant.app.main"]
    stale_entry = compile("print('stale')", "entry", "exec", optimize=1)
    _, errors = audit.audit_modules(tmp_path, modules, stale_entry)
    assert "Missing application module: wuwa_assistant.app.main" in errors
    assert "Application entry point differs from source" in errors


def test_resource_audit_covers_new_assets_and_migrations(tmp_path: Path) -> None:
    source_root, internal_root = tmp_path / "src", tmp_path / "package"
    asset = "wuwa_assistant/data/planning/new-catalog.json"
    migration = "wuwa_assistant/storage/migrations/versions/new_migration.py"
    for name in (asset, migration):
        _write(source_root, name)
        _write(internal_root, name)
    for name in audit.REQUIRED_RUNTIME_FILES:
        _write(internal_root, name)
    hashes, errors = audit.audit_resources(source_root, internal_root)
    assert not errors
    assert set(hashes) == {asset, migration}

    (internal_root / asset).write_text("stale", encoding="utf-8")
    (internal_root / migration).unlink()
    _, errors = audit.audit_resources(source_root, internal_root)
    assert f"Resource differs from source: {asset}" in errors
    assert f"Missing resource: {migration}" in errors


def test_binary_audit_checks_origin_content_and_unlisted_dlls(tmp_path: Path) -> None:
    runtime, external, package = tmp_path / "runtime", tmp_path / "external", tmp_path / "package"
    input_binary = _write(runtime, "valid.dll")
    foreign_binary = _write(external, "foreign.dll")
    _write(package, "valid.dll")
    _write(package, "foreign.dll")
    _write(package, "extra.pyd")
    _write(package, "nested/ICUUC.dll")
    inventory = {"valid.dll": input_binary, "foreign.dll": foreign_binary}
    _, errors = audit.audit_binaries(package, inventory, (runtime,))
    assert "Binary comes from outside the build runtime: foreign.dll" in errors
    assert "Unexpected binary absent from build inventory: extra.pyd" in errors
    assert "Unexpected ICU binary: nested/ICUUC.dll" in errors

    (package / "valid.dll").write_bytes(b"modified")
    _, errors = audit.audit_binaries(package, inventory, (runtime,))
    assert "Binary differs from build input: valid.dll" in errors


def test_binary_audit_rejects_missing_payload_and_empty_inventory(tmp_path: Path) -> None:
    source = _write(tmp_path / "runtime", "valid.dll")
    _, errors = audit.audit_binaries(tmp_path / "package", {"valid.dll": source}, (source.parent,))
    assert errors == ["Missing binary: valid.dll"]
    _, errors = audit.audit_binaries(tmp_path / "package", {}, (source.parent,))
    assert errors == ["Build binary inventory is empty"]


def test_binary_inventory_reads_nested_toc_without_treating_data_as_binaries(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "native.dll"
    toc = [[], [("native.dll", str(binary), "BINARY")], [("catalog.json", "input", "DATA")]]
    assert audit.binary_inventory(toc) == {"native.dll": binary}


def test_environment_manifest_contains_versions_without_installation_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        metadata: ClassVar = {"Name": "Example-Dependency"}
        version = "1.2.3"

    monkeypatch.setattr(audit.importlib.metadata, "distributions", lambda: [Distribution()])
    report = audit.build_environment()
    assert report["dependencies"] == [{"name": "Example-Dependency", "version": "1.2.3"}]
    assert set(report) == {"python", "platform", "architecture", "dependencies"}
