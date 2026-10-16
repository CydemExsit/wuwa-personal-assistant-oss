# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder build for the offline Windows desktop application."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, get_package_paths


# PyInstaller exposes SPECPATH as the directory containing this spec file.
project_root = Path(SPECPATH).resolve().parent
source_root = project_root / "src"
sys.path.insert(0, str(source_root))
sys.path.insert(0, str(project_root / "scripts"))
from wuwa_assistant.packaging_policy import excluded_binary
from package_notices import collect_notices
entry_point = source_root / "wuwa_assistant" / "app" / "__main__.py"


def data_file(source: Path, destination: str) -> tuple[str, str]:
    if not source.is_file():
        raise SystemExit(f"Required package data is missing: {source}")
    return (str(source), destination)


_rapidocr_package_root = Path(get_package_paths("rapidocr")[1])
_rapidocr_models = _rapidocr_package_root / "models"
_required_models = (
    "PP-OCRv6_det_small.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "PP-OCRv6_rec_small.onnx",
)

datas = [
    data_file(project_root / "LICENSE", "legal"),
    data_file(project_root / "THIRD_PARTY_NOTICES.md", "legal"),
    data_file(_rapidocr_package_root / "config.yaml", "rapidocr"),
    data_file(_rapidocr_package_root / "default_models.yaml", "rapidocr"),
]
datas.extend(collect_notices(project_root))
datas.extend(data_file(_rapidocr_models / name, "rapidocr/models") for name in _required_models)
datas.extend(
    collect_data_files(
        "wuwa_assistant.data.characters", includes=["*.json", "portraits/*.json"]
    )
)
datas.extend(collect_data_files("wuwa_assistant.data.scoring", includes=["*.json"]))
datas.extend(collect_data_files("wuwa_assistant.data.planning", includes=["*.json"]))
datas.extend(collect_data_files("wuwa_assistant.ui.styles", includes=["*.qss", "*.svg", "*.ico"]))
datas.extend(collect_data_files("tzdata"))
# The application is Traditional Chinese. Avoid shipping every Qt language pack.
datas.extend(collect_data_files("PySide6", includes=["translations/qtbase_zh_TW.qm"]))

_migration_root = source_root / "wuwa_assistant" / "storage" / "migrations"
for migration_file in sorted(_migration_root.rglob("*")):
    if migration_file.is_file() and migration_file.suffix in {".py", ".mako"}:
        relative_parent = migration_file.parent.relative_to(source_root)
        datas.append((str(migration_file), str(relative_parent).replace("\\", "/")))

hiddenimports = [
    # RapidOCR exposes its implementation through importlib-backed lazy attributes.
    "rapidocr.main",
    "rapidocr.inference_engine.onnxruntime",
    "rapidocr.inference_engine.onnxruntime.main",
    # Platform backends are selected locally at runtime without process attachment.
    "mss.windows",
    "sqlalchemy.dialects.sqlite",
]

analysis = Analysis(
    [str(entry_point)],
    pathex=[str(source_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "paddle",
        "paddleocr",
        "torch",
        "torchvision",
        "openvino",
        "tensorrt",
        "MNN",
    ],
    noarchive=False,
    optimize=1,
)

# Qt's generic hooks also collect an unused virtual keyboard, QML/PDF plugins
# and OpenCV's video codecs. This app uses Widgets/static images only. Keep the
# exclusion policy shared with the independent package audit, including its TOC.
analysis.binaries = [entry for entry in analysis.binaries if not excluded_binary(entry[0])]

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="WuwaDisplayAssistant",
    icon=str(source_root / "wuwa_assistant" / "ui" / "styles" / "app.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WuwaDisplayAssistant",
)
