from __future__ import annotations

from pathlib import Path

import pytest

from wuwa_assistant.safety_scan import (
    main,
    scan_dependency_file,
    scan_project,
    scan_source_tree,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _write_source(source_root: Path, relative: str, content: str) -> Path:
    path = source_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _minimal_project(root: Path, dependencies: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "safety-fixture"\nversion = "0.0.0"\ndependencies = [{dependencies}]\n',
        encoding="utf-8",
    )
    _write_source(root / "src", "wuwa_assistant/__init__.py", '"""Safe fixture."""\n')


def test_dependency_scan_rejects_runtime_and_optional_input_packages(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'name = "bad-dependencies"\n'
        'version = "0.0.0"\n'
        'dependencies = ["PyAutoGUI>=0.9"]\n'
        "[project.optional-dependencies]\n"
        'unsafe = ["pynput~=1.7", "PYSIDE6>=6"]\n',
        encoding="utf-8",
    )

    violations, declaration_count = scan_dependency_file(pyproject)

    assert declaration_count == 3
    assert [(item.code, item.evidence.split(":")[-1].strip()) for item in violations] == [
        ("DEP001", "PyAutoGUI>=0.9"),
        ("DEP001", "pynput~=1.7"),
    ]


def test_source_scan_rejects_input_simulation_import(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/unsafe_input.py",
        "import pyautogui\n\npyautogui.click(100, 100)\n",
    )

    violations, file_count = scan_source_tree(source_root)

    assert file_count == 1
    assert "SRC110" in {item.code for item in violations}
    assert any("pyautogui" in item.evidence for item in violations)


def test_source_scan_rejects_process_memory_and_injection_apis(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/process_access.py",
        """import ctypes

kernel = ctypes.windll.kernel32
handle = kernel.OpenProcess(0x1F0FFF, False, 123)
kernel.ReadProcessMemory(handle, 0, None, 0, None)
kernel.CreateRemoteThread(handle, None, 0, None, None, 0, None)
""",
    )

    violations, _ = scan_source_tree(source_root)
    codes = {item.code for item in violations}

    assert {"SRC220", "SRC221", "SRC223"} <= codes


def test_source_scan_rejects_video_and_mss_disk_shortcuts(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/recording.py",
        """import cv2
import mss
from mss.tools import to_png

writer = cv2.VideoWriter("capture.mp4", 0, 30, (2560, 1440))
to_png(b"pixels", (2560, 1440), output="frame.png")
with mss.mss() as desktop:
    desktop.shot()
""",
    )

    violations, _ = scan_source_tree(source_root)
    codes = {item.code for item in violations}

    assert {"SRC520", "SRC521", "SRC522"} <= codes


def test_capture_code_cannot_write_full_frame_bytes(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/capture/live_backend.py",
        """from pathlib import Path

def persist_frame(frame: object) -> None:
    Path("full-frame.bin").write_bytes(frame.tobytes())
""",
    )

    violations, _ = scan_source_tree(source_root)

    assert any(item.code == "SRC524" and "write_bytes" in item.evidence for item in violations)


def test_capture_named_service_is_also_treated_as_live_capture_code(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/services/capture_service.py",
        'def persist() -> None:\n    open("frame.raw", "wb").write(b"pixels")\n',
    )

    violations, _ = scan_source_tree(source_root)

    assert any(item.code == "SRC524" for item in violations)


def test_existing_bounded_roi_debug_writer_pattern_is_allowed(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/vision/debug.py",
        """from pathlib import Path

import cv2

def save_roi(path: Path, roi: object) -> None:
    ok, encoded = cv2.imencode(".png", roi)
    if ok:
        path.write_bytes(encoded.tobytes())
""",
    )

    violations, _ = scan_source_tree(source_root)

    assert violations == []


def test_dynamic_forbidden_api_string_is_detected_but_docstring_is_not(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/dynamic.py",
        '"""This safety note may mention OpenProcess without invoking it."""\n'
        "import ctypes\n\n"
        'dangerous = getattr(ctypes.windll.kernel32, "OpenProcess")\n',
    )

    violations, _ = scan_source_tree(source_root)

    assert any(item.code == "SRC230" for item in violations)
    assert sum(item.code == "SRC230" for item in violations) == 1


def test_packet_capture_and_click_through_overlay_symbols_are_rejected(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/unsafe_observer.py",
        """import socket
from PySide6.QtCore import Qt
from scapy.all import sniff

raw_socket = socket.socket(socket.AF_INET, socket.SOCK_RAW)
overlay_flag = Qt.WindowType.WindowTransparentForInput
sniff()
""",
    )

    violations, _ = scan_source_tree(source_root)
    codes = {item.code for item in violations}

    assert {"SRC310", "SRC320", "SRC422"} <= codes


def test_current_repository_passes_all_safety_rules() -> None:
    result = scan_project(REPOSITORY_ROOT)

    assert result.violations == ()
    assert result.dependency_declarations > 0
    assert result.source_files > 0


def test_panel_exception_allows_only_own_modules_and_capture_affinity(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        source_root,
        "wuwa_assistant/ui/native_panel.py",
        'import ctypes\nuser32 = ctypes.WinDLL("user32")\n'
        "user32.SetWindowDisplayAffinity(own_handle, 0x11)\n"
        "user32.GetWindowDisplayAffinity(own_handle, result)\n",
    )
    _write_source(
        source_root,
        "wuwa_assistant/ui/echo_panel.py",
        "from PySide6.QtCore import Qt\nflag = Qt.WindowType.WindowTransparentForInput\n",
    )
    assert scan_source_tree(source_root)[0] == []
    _write_source(
        source_root,
        "wuwa_assistant/ui/other_panel.py",
        "import ctypes\nfrom PySide6.QtCore import Qt\n"
        'user32 = ctypes.WinDLL("user32")\n'
        "user32.SetWindowDisplayAffinity(own_handle, 0x11)\n"
        "flag = Qt.WindowType.WindowTransparentForInput\n",
    )
    assert {"SRC226", "SRC422", "SRC423"} <= {
        item.code for item in scan_source_tree(source_root)[0]
    }


@pytest.mark.parametrize(
    "source",
    [
        'import ctypes\nctypes.WinDLL("kernel32")\n',
        'user32.FindWindowW(None, "game")\n',
        "user32.SendInput(1, None, 0)\n",
        "user32.GetForegroundWindow()\n",
        "user32.SetWindowsHookExW(None)\n",
    ],
)
def test_panel_exception_still_denies_other_native_apis(tmp_path: Path, source: str) -> None:
    source_root = tmp_path / "src"
    _write_source(source_root, "wuwa_assistant/ui/native_panel.py", source)
    assert scan_source_tree(source_root)[0]


def test_cli_has_clear_success_and_failure_exit_codes(tmp_path: Path, capsys: object) -> None:
    safe_root = tmp_path / "safe"
    _minimal_project(safe_root)

    assert main(["--root", str(safe_root)]) == 0
    success_output = capsys.readouterr()  # type: ignore[attr-defined]
    assert "Safety scan passed" in success_output.out

    unsafe_root = tmp_path / "unsafe"
    _minimal_project(unsafe_root, '"vgamepad>=0.1"')

    assert main(["--root", str(unsafe_root)]) == 1
    failure_output = capsys.readouterr()  # type: ignore[attr-defined]
    assert "Safety scan FAILED" in failure_output.err
    assert "DEP001" in failure_output.err
