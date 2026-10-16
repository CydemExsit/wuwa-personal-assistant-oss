"""Per-user Windows Task Scheduler integration; never needs saved credentials."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


@dataclass(frozen=True, slots=True)
class ReminderCommand:
    executable: Path
    arguments: tuple[str, ...]
    working_directory: Path


def current_command(data_root: Path) -> ReminderCommand:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        prefix: tuple[str, ...] = ()
    else:
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            executable = windowless
        prefix = ("-m", "wuwa_assistant.app")
    return ReminderCommand(
        executable,
        (*prefix, "--data-root", str(data_root.resolve()), "--check-reminders"),
        Path.cwd().resolve() if prefix else executable.parent,
    )


def task_name(data_root: Path) -> str:
    digest = hashlib.sha256(str(data_root.resolve()).casefold().encode()).hexdigest()[:12]
    return f"WuwaDisplayAssistant-Weekend-{digest}"


def task_xml(command: ReminderCommand, *, hour: int, user: str, minute: int = 0) -> str:
    """A zoned weekly trigger plus logon catch-up; the runner rechecks eligibility."""
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("reminder hour must be in 0..23")
    if type(minute) is not int or not 0 <= minute <= 59:
        raise ValueError("reminder minute must be in 0..59")
    if not user.strip() or not command.executable.is_absolute():
        raise ValueError("a Windows user and absolute executable are required")
    ET.register_namespace("", TASK_NAMESPACE)

    def node(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
        item = ET.SubElement(parent, f"{{{TASK_NAMESPACE}}}{name}")
        item.text = text
        return item

    root = ET.Element(f"{{{TASK_NAMESPACE}}}Task", {"version": "1.2"})
    registration = node(root, "RegistrationInfo")
    node(registration, "Description", "鳴潮週末提醒;只讀公開活動與本機完成記錄。")
    triggers = node(root, "Triggers")
    calendar = node(triggers, "CalendarTrigger")
    node(calendar, "StartBoundary", f"2026-01-03T{hour:02d}:{minute:02d}:00+08:00")
    node(calendar, "Enabled", "true")
    schedule = node(calendar, "ScheduleByWeek")
    node(schedule, "WeeksInterval", "1")
    days = node(schedule, "DaysOfWeek")
    node(days, "Saturday")
    node(days, "Sunday")
    logon = node(triggers, "LogonTrigger")
    node(logon, "Enabled", "true")
    node(logon, "UserId", user)
    principals = node(root, "Principals")
    principal = node(principals, "Principal")
    principal.set("id", "Author")
    node(principal, "UserId", user)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(root, "Settings")
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "true"),
        ("ExecutionTimeLimit", "PT5M"),
    ):
        node(settings, key, value)
    actions = node(root, "Actions")
    actions.set("Context", "Author")
    action = node(actions, "Exec")
    node(action, "Command", str(command.executable))
    node(action, "Arguments", subprocess.list2cmdline(command.arguments))
    node(action, "WorkingDirectory", str(command.working_directory))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode")


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    if sys.platform != "win32":
        raise OSError("背景提醒目前只支援 Windows。")
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("Windows 排程操作未完成, 請稍後重新套用提醒設定。") from exc


def configure_task(data_root: Path, *, enabled: bool, hour: int = 10, minute: int = 0) -> str:
    name = task_name(data_root)
    # Address only this data root's task; never edit unrelated scheduler entries.
    if not enabled:
        found = _run(["schtasks.exe", "/Query", "/TN", name])
        if found.returncode == 0:
            result = _run(["schtasks.exe", "/Delete", "/TN", name, "/F"])
            if result.returncode:
                raise OSError("無法停用背景提醒:" + result.stderr.strip())
        return "已停用週末背景提醒。"
    command = current_command(data_root)
    if not command.executable.is_file():
        raise OSError("找不到提醒程式,請由有效的安裝位置重新設定。")
    identity = _run(["whoami.exe"])
    if identity.returncode or not identity.stdout.strip():
        raise OSError("無法確認目前 Windows 使用者。")
    xml = task_xml(command, hour=hour, minute=minute, user=identity.stdout.strip())
    task_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as handle:
            task_path = Path(handle.name)
            handle.write(xml.encode("utf-16"))
        result = _run(["schtasks.exe", "/Create", "/TN", name, "/XML", str(task_path), "/F"])
        if result.returncode:
            raise OSError("Windows 未能建立背景提醒:" + result.stderr.strip())
        verified = _run(["schtasks.exe", "/Query", "/TN", name, "/XML"])
        if verified.returncode:
            raise OSError("提醒建立後未能驗證,請檢查 Windows 工作排程器。")
        actual = ET.fromstring(verified.stdout)
        ns = {"t": TASK_NAMESPACE}
        actual_command = actual.findtext(".//t:Exec/t:Command", namespaces=ns)
        actual_arguments = actual.findtext(".//t:Exec/t:Arguments", namespaces=ns)
        if actual_command != str(command.executable) or actual_arguments != subprocess.list2cmdline(
            command.arguments
        ):
            raise OSError("提醒排程指向不符,未套用設定。")
    finally:
        if task_path is not None:
            task_path.unlink(missing_ok=True)
    return f"已啟用:每週六、日 {hour:02d}:{minute:02d}(UTC+8);程式關閉後仍可提醒。"


def eligible_weekend(now: datetime, hour: int, minute: int = 0) -> bool:
    from zoneinfo import ZoneInfo

    if now.tzinfo is None:
        raise ValueError("aware datetime required")
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("reminder hour must be in 0..23")
    if type(minute) is not int or not 0 <= minute <= 59:
        raise ValueError("reminder minute must be in 0..59")
    local = now.astimezone(ZoneInfo("Asia/Taipei"))
    return local.weekday() in (5, 6) and (local.hour, local.minute) >= (hour, minute)
