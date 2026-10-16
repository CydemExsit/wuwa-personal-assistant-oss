from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from wuwa_assistant.packaging_policy import excluded_binary


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / f"scripts/{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_script("public_safety_audit")
audit = load_script("package_audit")
sources = load_script("third_party_sources")


def test_source_bundle_rejects_runtime_version_drift():
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "third_party/upstream-sources.json").read_text()
    )
    sources.verify_runtime_versions(manifest["runtime_versions"])
    manifest["runtime_versions"]["GEOS"] = "0.0.0"
    with pytest.raises(RuntimeError, match="do not match build runtime"):
        sources.verify_runtime_versions(manifest["runtime_versions"])


@pytest.mark.parametrize(
    "name",
    [
        "samples_private/frame.png",
        "state.sqlite3",
        ".env",
        "work/report.json",
        "image.png",
        "application.exe",
        "credentials/session.json",
    ],
)
def test_public_guard_rejects_private_artifacts_even_if_force_tracked(name):
    assert guard.inspect_file(name, b"test")


def test_public_guard_checks_content_and_preserves_synthetic_redaction_tests():
    assert guard.inspect_file("data.bin", b"SQLite format 3\0")
    home_path = "C:" + "\\Users\\" + "PRIVATE_NAME\\file"
    assert guard.inspect_file("notes.md", home_path.encode()) == ["personal home path"]
    assert not guard.inspect_file("tests/redaction.py", b"D:\\private\\models\\secret.onnx")
    assert not guard.inspect_file("LICENSE", b"MIT License")


@pytest.mark.parametrize(
    "name",
    [
        "PySide6/Qt6VirtualKeyboard.dll",
        "qtvirtualkeyboardplugin.dll",
        "qpdf.dll",
        "Qt6QmlModels.dll",
        "Qt6Quick.dll",
        "opencv_videoio_ffmpeg999_64.dll",
        "PySide6/opengl32sw.dll",
    ],
)
def test_unused_native_payload_is_rejected_even_if_present_in_build_inventory(tmp_path, name):
    assert excluded_binary(name)
    source = tmp_path / "runtime.dll"
    source.write_bytes(b"library")
    target = tmp_path / "package" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    _, errors = audit.audit_binaries(tmp_path / "package", {name: source}, (tmp_path,))
    assert f"Unsupported optional binary: {name}" in errors
    assert not excluded_binary("PySide6/Qt6Widgets.dll")


def test_legal_audit_detects_missing_tampered_and_escaping_notices(tmp_path):
    _, errors = audit.audit_legal_files(tmp_path)
    assert "Missing legal file: LICENSE" in errors
    legal = tmp_path / "legal"
    legal.mkdir()
    (legal / "LICENSE_MANIFEST.json").write_text(
        json.dumps({"files": {"missing.txt": "0" * 64, "../outside.txt": "0" * 64}})
    )
    _, errors = audit.audit_legal_files(tmp_path)
    assert "License differs from manifest: missing.txt" in errors
    assert "Invalid legal manifest path" in errors


def test_model_provenance_checks_bytes_not_just_model_filename(tmp_path, monkeypatch):
    good = b"synthetic model bytes"
    monkeypatch.setattr(audit, "MODEL_HASHES", {"model.onnx": hashlib.sha256(good).hexdigest()})
    model = tmp_path / "rapidocr/models/model.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(good)
    _, errors = audit.audit_model_hashes(tmp_path)
    assert errors == []
    model.write_bytes(b"different bytes")
    _, errors = audit.audit_model_hashes(tmp_path)
    assert errors == ["OCR model differs from upstream provenance: model.onnx"]
