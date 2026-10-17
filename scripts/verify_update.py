"""Exercise the real Setup parent-exit wait, restart and isolated-data retention."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def logical_snapshot(path: Path) -> tuple[str, ...]:
    # Opening/closing the shell refreshes only these window bookkeeping times.
    # Normalize them in a disposable in-memory copy; compare every stored value,
    # schema, user record and other timestamp exactly. Never rewrite test data.
    with sqlite3.connect(path) as source, sqlite3.connect(":memory:") as snapshot:
        source.backup(snapshot)
        snapshot.execute(
            "UPDATE app_settings SET updated_at = '' WHERE key IN (?, ?, ?)",
            ("ui/last_page", "ui/main_window_geometry", "ui/main_window_state"),
        )
        return tuple(snapshot.iterdump())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--installed-executable", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve()
    workspace = Path(__file__).resolve().parents[1]
    if not root.is_relative_to(workspace / "work") or not root.parent.name.startswith(
        "installer-test-"
    ):
        raise ValueError("Update verification only writes isolated installer-test data in work/")
    from wuwa_assistant.application import DemoDataService
    from wuwa_assistant.storage import AppPaths, create_database

    with create_database(AppPaths.from_root(root)) as database:
        demo = DemoDataService(database.session_factory).ensure_demo_data()
        assert demo.character_count == 2 and demo.team_count == 1
        database.settings.set("update_verification/preserved", {"value": "synthetic setting"})
    backup = root / "backups" / "update-retention-sentinel.json"
    backup.write_text('{"synthetic_backup":"preserve"}', encoding="utf-8")
    db = root / "data" / "wuwa-personal-assistant.sqlite3"
    before = logical_snapshot(db)
    # Only the installed test application writes this log. Its new stop entry
    # proves that Setup reopened the executable with the same explicit data root.
    log = root / "logs" / "application.log"
    original_log = log.read_bytes()
    setup_log = root.parent / "update-setup.log"
    command = [
        str(args.installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/WUWAUPDATE=1",
        "/WUWASMOKETEST=1",
        f"/WUWADATA={root}",
        f"/DIR={args.installed_executable.parent}",
        f"/LOG={setup_log}",
    ]
    release_parent = root.parent / "update-parent-release.txt"
    code = """
import sys,time
from pathlib import Path
while not Path(sys.argv[1]).exists(): time.sleep(0.05)
"""
    parent = subprocess.Popen([sys.executable, "-c", code, str(release_parent)])
    setup = subprocess.Popen([*command, f"/WUWAUPDATEPID={parent.pid}"])
    time.sleep(2)
    if parent.poll() is not None or log.read_bytes() != original_log:
        raise RuntimeError("Setup did not wait for its requesting parent")
    if setup_log.exists() and "Installing the file" in setup_log.read_text(
        "utf-8-sig", errors="replace"
    ):
        raise RuntimeError("Setup installed files while its parent was still active")
    release_parent.touch()
    parent.wait(timeout=10)
    assert setup.wait(timeout=120) == 0, "Setup update failed"
    # Wait for the actual reopened frozen application, not just Setup's exit.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = log.read_bytes()
        if (
            len(current) > len(original_log)
            and b"application_stopped" in current[len(original_log) :]
        ):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("Updated application did not reopen and finish the smoke check")
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert logical_snapshot(db) == before, "Update/reopen changed logical user data"
    assert backup.read_text("utf-8") == '{"synthetic_backup":"preserve"}'
    report = {
        "status": "passed",
        "parent_exit_wait": "passed",
        "reopened_same_data_root": "passed",
        "logical_data_preserved": "passed",
        "integrity": "passed",
        "foreign_keys": "passed",
        "synthetic_characters": 2,
        "synthetic_teams": 1,
        "backup_preserved": "passed",
    }
    output = args.installer.parent / "reports" / "update-validation.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Real installer update, parent exit, reopened application and data retention passed.")


if __name__ == "__main__":
    main()
