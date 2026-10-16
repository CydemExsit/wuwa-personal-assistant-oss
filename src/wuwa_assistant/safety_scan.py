"""Static safety boundary enforcement for dependencies and application source.

This scanner is intentionally conservative around game input, process access,
injection, overlays, packet inspection, video recording, and full-frame writes.
It does not execute project code or inspect user data.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_NORMALIZE_DISTRIBUTION = re.compile(r"[-_.]+")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

DENIED_DISTRIBUTIONS = frozenset(
    {
        "ahk",
        "autopy",
        "frida",
        "frida-tools",
        "interception-python",
        "keyboard",
        "mitmproxy",
        "mouse",
        "mousekey",
        "pydirectinput",
        "pyinjector",
        "pynput",
        "pyautogui",
        "pyuserinput",
        "pyshark",
        "pyvjoy",
        "pywinauto",
        "pymem",
        "scapy",
        "uiautomation",
        "vgamepad",
        "winappdbg",
    }
)

_INPUT_MODULES = (
    "ahk",
    "autopy",
    "keyboard",
    "mouse",
    "pydirectinput",
    "pynput",
    "pyautogui",
    "pyuserinput",
    "pywinauto",
    "uiautomation",
    "vgamepad",
)
_PROCESS_MODULES = (
    "frida",
    "pymem",
    "pyinjector",
    "psutil",
    "ptrace",
    "win32gui",
    "win32process",
    "winappdbg",
)
_PACKET_MODULES = (
    "dpkt",
    "mitmproxy",
    "pcapy",
    "pyshark",
    "scapy",
)

_DANGEROUS_CALL_NAMES: dict[str, tuple[str, str]] = {
    # Input and game/window control.
    "SendInput": ("SRC120", "input simulation API is forbidden"),
    "keybd_event": ("SRC120", "keyboard simulation API is forbidden"),
    "mouse_event": ("SRC120", "mouse simulation API is forbidden"),
    "SetCursorPos": ("SRC120", "cursor control API is forbidden"),
    "SendMessage": ("SRC121", "window message control is forbidden"),
    "SendMessageW": ("SRC121", "window message control is forbidden"),
    "PostMessage": ("SRC121", "window message control is forbidden"),
    "PostMessageW": ("SRC121", "window message control is forbidden"),
    # Process handles, memory, injection, and hooks.
    "OpenProcess": ("SRC220", "opening a process handle is forbidden"),
    "ReadProcessMemory": ("SRC221", "process memory access is forbidden"),
    "WriteProcessMemory": ("SRC221", "process memory access is forbidden"),
    "NtReadVirtualMemory": ("SRC221", "process memory access is forbidden"),
    "NtWriteVirtualMemory": ("SRC221", "process memory access is forbidden"),
    "VirtualAllocEx": ("SRC222", "remote process allocation is forbidden"),
    "VirtualProtectEx": ("SRC222", "remote process modification is forbidden"),
    "CreateRemoteThread": ("SRC223", "remote thread injection is forbidden"),
    "CreateToolhelp32Snapshot": ("SRC224", "process discovery is forbidden"),
    "Process32First": ("SRC224", "process discovery is forbidden"),
    "Process32Next": ("SRC224", "process discovery is forbidden"),
    "GetWindowThreadProcessId": ("SRC224", "window-to-process discovery is forbidden"),
    "SetWindowsHookEx": ("SRC225", "Windows hook installation is forbidden"),
    "SetWindowsHookExA": ("SRC225", "Windows hook installation is forbidden"),
    "SetWindowsHookExW": ("SRC225", "Windows hook installation is forbidden"),
    "SetWinEventHook": ("SRC225", "Windows event hooks are forbidden"),
    "WinDLL": ("SRC226", "direct Win32 DLL access is forbidden in project source"),
    "SetWindowDisplayAffinity": ("SRC423", "capture exclusion is restricted to our own panel"),
    "GetWindowDisplayAffinity": ("SRC423", "capture exclusion is restricted to our own panel"),
    # Game-window attachment and overlay primitives.
    "FindWindow": ("SRC420", "game/window discovery is forbidden"),
    "FindWindowA": ("SRC420", "game/window discovery is forbidden"),
    "FindWindowW": ("SRC420", "game/window discovery is forbidden"),
    "EnumWindows": ("SRC420", "game/window discovery is forbidden"),
    "SetParent": ("SRC421", "attaching to another window is forbidden"),
    "SetWindowLong": ("SRC421", "native overlay/window mutation is forbidden"),
    "SetWindowLongA": ("SRC421", "native overlay/window mutation is forbidden"),
    "SetWindowLongW": ("SRC421", "native overlay/window mutation is forbidden"),
    "UpdateLayeredWindow": ("SRC421", "layered overlay rendering is forbidden"),
    "WindowTransparentForInput": ("SRC422", "click-through overlay behavior is forbidden"),
    "WA_TransparentForMouseEvents": ("SRC422", "click-through overlay behavior is forbidden"),
    "WS_EX_TRANSPARENT": ("SRC422", "click-through overlay behavior is forbidden"),
    # Recording and mss persistence shortcuts.
    "VideoWriter": ("SRC520", "video recording is forbidden"),
    "to_png": ("SRC521", "mss PNG persistence shortcut is forbidden"),
}

_DANGEROUS_SYMBOL_NAMES: dict[str, tuple[str, str]] = {
    "AF_PACKET": ("SRC320", "raw packet capture is forbidden"),
    "PROCESS_ALL_ACCESS": ("SRC227", "process handle access flags are forbidden"),
    "PROCESS_VM_OPERATION": ("SRC227", "process memory flags are forbidden"),
    "PROCESS_VM_READ": ("SRC227", "process memory flags are forbidden"),
    "PROCESS_VM_WRITE": ("SRC227", "process memory flags are forbidden"),
    "SIO_RCVALL": ("SRC320", "raw packet capture is forbidden"),
    "SOCK_RAW": ("SRC320", "raw packet capture is forbidden"),
    "WA_TransparentForMouseEvents": (
        "SRC422",
        "click-through overlay behavior is forbidden",
    ),
    "WS_EX_TRANSPARENT": ("SRC422", "click-through overlay behavior is forbidden"),
    "WindowTransparentForInput": (
        "SRC422",
        "click-through overlay behavior is forbidden",
    ),
}

_NON_DOCSTRING_TEXT_MARKERS: dict[str, tuple[str, str]] = {
    "autohotkey": ("SRC130", "AutoHotkey invocation is forbidden"),
    "sendinput": ("SRC130", "dynamic input simulation is forbidden"),
    "openprocess": ("SRC230", "dynamic process handle access is forbidden"),
    "readprocessmemory": ("SRC230", "dynamic process memory access is forbidden"),
    "writeprocessmemory": ("SRC230", "dynamic process memory access is forbidden"),
    "createremotethread": ("SRC230", "dynamic process injection is forbidden"),
    "setwindowshookex": ("SRC230", "dynamic hook installation is forbidden"),
    "ws_ex_transparent": ("SRC430", "dynamic click-through overlay behavior is forbidden"),
    "mss.tools.to_png": ("SRC530", "mss PNG persistence shortcut is forbidden"),
}

_GLOBAL_IMAGE_WRITE_SUFFIXES = (
    "cv2.imencode",
    "cv2.imwrite",
    "imageio.imwrite",
    "numpy.save",
    "numpy.savez",
    "numpy.savez_compressed",
)
_CAPTURE_WRITE_NAMES = frozenset(
    {
        "dump",
        "imencode",
        "imwrite",
        "save",
        "savez",
        "savez_compressed",
        "tofile",
        "write_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class SafetyViolation:
    code: str
    path: Path
    line: int
    message: str
    evidence: str

    def render(self, root: Path) -> str:
        try:
            display_path = self.path.resolve().relative_to(root.resolve())
        except ValueError:
            display_path = self.path
        location = str(display_path) if self.line <= 0 else f"{display_path}:{self.line}"
        return f"[{self.code}] {location}: {self.message} ({self.evidence})"


@dataclass(frozen=True, slots=True)
class SafetyScanResult:
    root: Path
    dependency_declarations: int
    source_files: int
    violations: tuple[SafetyViolation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations


def _normalize_distribution(name: str) -> str:
    return _NORMALIZE_DISTRIBUTION.sub("-", name).casefold()


def _dependency_name(requirement: str) -> str | None:
    match = _REQUIREMENT_NAME.match(requirement)
    return _normalize_distribution(match.group(1)) if match else None


def scan_dependency_file(path: Path) -> tuple[list[SafetyViolation], int]:
    """Scan direct runtime and optional project dependencies in one pyproject file."""

    if not path.is_file():
        return (
            [
                SafetyViolation(
                    "CFG001",
                    path,
                    0,
                    "pyproject.toml is missing",
                    "dependency scan could not run",
                )
            ],
            0,
        )

    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return (
            [SafetyViolation("CFG002", path, 0, "cannot parse pyproject.toml", str(exc))],
            0,
        )

    project = payload.get("project", {})
    declarations: list[tuple[str, str]] = []
    if isinstance(project, dict):
        dependencies = project.get("dependencies", [])
        if isinstance(dependencies, list):
            declarations.extend(
                ("project.dependencies", item) for item in dependencies if isinstance(item, str)
            )
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group, values in optional.items():
                if isinstance(values, list):
                    declarations.extend(
                        (f"project.optional-dependencies.{group}", item)
                        for item in values
                        if isinstance(item, str)
                    )

    violations: list[SafetyViolation] = []
    for section, declaration in declarations:
        name = _dependency_name(declaration)
        if name in DENIED_DISTRIBUTIONS:
            violations.append(
                SafetyViolation(
                    "DEP001",
                    path,
                    0,
                    f"forbidden safety-boundary dependency {name!r}",
                    f"{section}: {declaration}",
                )
            )
    return violations, len(declarations)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            result.add(id(first.value))
    return result


class _SourceSafetyVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, relative_path: Path, tree: ast.AST) -> None:
        self.path = path
        self.relative_path = relative_path
        self.aliases = self._collect_aliases(tree)
        self.imported_modules = set(self.aliases.values())
        self.docstring_nodes = _docstring_node_ids(tree)
        self.violations: list[SafetyViolation] = []
        self._dedupe: set[tuple[str, int, str]] = set()
        parts = {part.casefold() for part in relative_path.parts}
        stem = relative_path.stem.casefold()
        self.capture_or_live = bool(
            {"capture", "live"} & parts or "capture" in stem or "live" in stem
        )
        self.allowed_debug_writer = relative_path.as_posix().casefold().endswith("vision/debug.py")
        self.panel_ui = relative_path.as_posix() == "wuwa_assistant/ui/echo_panel.py"
        self.panel_native = relative_path.as_posix() == "wuwa_assistant/ui/native_panel.py"

    def _panel_exception(self, symbol: str) -> bool:
        return (self.panel_ui and symbol == "WindowTransparentForInput") or (
            self.panel_native
            and symbol
            in {
                "WinDLL",
                "SetWindowDisplayAffinity",
                "GetWindowDisplayAffinity",
            }
        )

    @staticmethod
    def _collect_aliases(tree: ast.AST) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split(".")[0]] = item.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
        return aliases

    def _resolve(self, name: str | None) -> str | None:
        if not name:
            return None
        first, separator, remainder = name.partition(".")
        resolved = self.aliases.get(first, first)
        return f"{resolved}.{remainder}" if separator else resolved

    def _add(self, code: str, node: ast.AST, message: str, evidence: str) -> None:
        line = getattr(node, "lineno", 0)
        key = (code, line, evidence)
        if key in self._dedupe:
            return
        self._dedupe.add(key)
        self.violations.append(SafetyViolation(code, self.path, line, message, evidence[:200]))

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self._check_module(item.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self._check_module(module, node)
        if module == "mss.tools" and any(item.name == "to_png" for item in node.names):
            self._add(
                "SRC521",
                node,
                "mss PNG persistence shortcut is forbidden",
                "from mss.tools import to_png",
            )
        self.generic_visit(node)

    def _check_module(self, module: str, node: ast.AST) -> None:
        if any(module == item or module.startswith(f"{item}.") for item in _INPUT_MODULES):
            self._add("SRC110", node, "input-simulation module is forbidden", module)
        elif any(module == item or module.startswith(f"{item}.") for item in _PROCESS_MODULES):
            self._add("SRC210", node, "process/memory access module is forbidden", module)
        elif any(module == item or module.startswith(f"{item}.") for item in _PACKET_MODULES):
            self._add("SRC310", node, "packet interception module is forbidden", module)

    def visit_Call(self, node: ast.Call) -> None:
        raw_name = _qualified_name(node.func)
        resolved_name = self._resolve(raw_name)
        last_name = (resolved_name or raw_name or "").rsplit(".", 1)[-1]

        denied_call = _DANGEROUS_CALL_NAMES.get(last_name)
        if denied_call is not None and not self._panel_exception(last_name):
            code, message = denied_call
            self._add(code, node, message, resolved_name or last_name)

        if (
            self.panel_native
            and last_name == "WinDLL"
            and (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or node.args[0].value != "user32"
            )
        ):
            self._add("SRC424", node, "panel may load only user32", resolved_name or last_name)

        if self._imports_mss() and last_name == "shot":
            self._add(
                "SRC522",
                node,
                "mss shot() writes a full monitor image and is forbidden",
                resolved_name or "shot",
            )

        if (
            not self.allowed_debug_writer
            and resolved_name is not None
            and any(resolved_name.endswith(item) for item in _GLOBAL_IMAGE_WRITE_SUFFIXES)
        ):
            self._add(
                "SRC523",
                node,
                "image persistence outside the bounded ROI DebugWriter is forbidden",
                resolved_name,
            )

        if not self.allowed_debug_writer and last_name == "write_bytes":
            nested_names = {
                (_qualified_name(item.func) or "").rsplit(".", 1)[-1]
                for argument in (*node.args, *(item.value for item in node.keywords))
                for item in ast.walk(argument)
                if isinstance(item, ast.Call)
            }
            if "tobytes" in nested_names:
                self._add(
                    "SRC523",
                    node,
                    "serialized image/frame bytes may not be written outside DebugWriter",
                    resolved_name or last_name,
                )

        if (
            not self.allowed_debug_writer
            and last_name in {"save", "tofile"}
            and self._imports_image_array_module()
        ):
            self._add(
                "SRC523",
                node,
                "image/array persistence outside DebugWriter is forbidden",
                resolved_name or last_name,
            )

        if self.capture_or_live and last_name in _CAPTURE_WRITE_NAMES:
            self._add(
                "SRC524",
                node,
                "capture/live code may not persist frame bytes",
                resolved_name or last_name,
            )
        if self.capture_or_live and last_name == "open" and self._is_binary_write_open(node):
            self._add(
                "SRC524",
                node,
                "capture/live code may not open a binary frame output",
                resolved_name or "open",
            )

        if last_name in {"getattr", "GetProcAddress"}:
            for argument in (*node.args, *(item.value for item in node.keywords)):
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    self._check_non_docstring_text(argument.value, argument)

        self.generic_visit(node)

    @staticmethod
    def _is_binary_write_open(node: ast.Call) -> bool:
        mode: str | None = None
        if (
            len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            mode = node.args[1].value
        for keyword in node.keywords:
            if (
                keyword.arg == "mode"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                mode = keyword.value.value
        return bool(mode and "b" in mode and any(flag in mode for flag in "wax+"))

    def _imports_mss(self) -> bool:
        return any(module == "mss" or module.startswith("mss.") for module in self.imported_modules)

    def _imports_image_array_module(self) -> bool:
        return any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in self.imported_modules
            for prefix in ("PIL", "cv2", "imageio", "numpy")
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            self.panel_native
            and isinstance(node.value, ast.Name)
            and node.value.id == "user32"
            and node.attr
            not in {
                "SetWindowDisplayAffinity",
                "GetWindowDisplayAffinity",
            }
        ):
            self._add("SRC424", node, "panel native API is not allowlisted", node.attr)
        denied = _DANGEROUS_SYMBOL_NAMES.get(node.attr)
        if denied is not None and not self._panel_exception(node.attr):
            code, message = denied
            self._add(code, node, message, _qualified_name(node) or node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        denied = _DANGEROUS_SYMBOL_NAMES.get(node.id)
        if denied is not None and not self._panel_exception(node.id):
            code, message = denied
            self._add(code, node, message, self._resolve(node.id) or node.id)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if id(node) not in self.docstring_nodes and isinstance(node.value, str):
            self._check_non_docstring_text(node.value, node)
        self.generic_visit(node)

    def _check_non_docstring_text(self, text: str, node: ast.AST) -> None:
        folded = text.casefold()
        for marker, (code, message) in _NON_DOCSTRING_TEXT_MARKERS.items():
            if marker in folded:
                self._add(code, node, message, marker)


def scan_source_tree(source_root: Path) -> tuple[list[SafetyViolation], int]:
    """AST-scan project Python source while allowing only the existing ROI DebugWriter."""

    if not source_root.is_dir():
        return (
            [SafetyViolation("CFG003", source_root, 0, "source directory is missing", "src")],
            0,
        )

    violations: list[SafetyViolation] = []
    files = sorted(source_root.rglob("*.py"))
    scanned_files = 0
    for path in files:
        relative_path = path.relative_to(source_root)
        # The scanner contains policy marker literals; it cannot meaningfully scan itself.
        if relative_path.as_posix() == "wuwa_assistant/safety_scan.py":
            continue
        scanned_files += 1
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            line = exc.lineno if isinstance(exc, SyntaxError) and exc.lineno else 0
            violations.append(
                SafetyViolation("SRC001", path, line, "cannot parse Python source", str(exc))
            )
            continue
        visitor = _SourceSafetyVisitor(path, relative_path, tree)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations, scanned_files


def scan_project(root: Path) -> SafetyScanResult:
    resolved = root.expanduser().resolve()
    dependency_violations, declaration_count = scan_dependency_file(resolved / "pyproject.toml")
    source_violations, file_count = scan_source_tree(resolved / "src")
    violations = sorted(
        (*dependency_violations, *source_violations),
        key=lambda item: (str(item.path).casefold(), item.line, item.code, item.evidence),
    )
    return SafetyScanResult(
        root=resolved,
        dependency_declarations=declaration_count,
        source_files=file_count,
        violations=tuple(violations),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m wuwa_assistant.safety_scan",
        description="Check permanent local-only, passive-observation safety boundaries",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root containing pyproject.toml and src/",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = scan_project(args.root)
    if result.passed:
        print("Safety scan passed.")
        print(f"  Direct dependency declarations checked: {result.dependency_declarations}")
        print(f"  Python source files checked: {result.source_files}")
        return 0

    print(f"Safety scan FAILED with {len(result.violations)} violation(s):", file=sys.stderr)
    for violation in result.violations:
        print(f"  {violation.render(result.root)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DENIED_DISTRIBUTIONS",
    "SafetyScanResult",
    "SafetyViolation",
    "main",
    "scan_dependency_file",
    "scan_project",
    "scan_source_tree",
]
