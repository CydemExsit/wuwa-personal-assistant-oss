"""Consistent, offline SQLite snapshots used by backup and restore workflows.

The helpers in this module never copy a live ``-wal``/``-shm`` pair.  They use
SQLite's online backup API to materialize one self-contained database and then
validate that snapshot before publishing it with an atomic rename.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine


class BackupError(RuntimeError):
    """Raised when a database cannot be snapshotted or safely validated."""


@dataclass(frozen=True, slots=True)
class DatabaseShape:
    """Schema identity used to reject mislabeled or foreign SQLite files."""

    schema_revision: str
    table_names: tuple[str, ...]
    schema_sha256: str


@dataclass(frozen=True, slots=True)
class SqliteSnapshotInfo:
    """Validated metadata for one self-contained SQLite snapshot."""

    path: Path
    size_bytes: int
    sha256: str
    schema_revision: str
    table_counts: tuple[tuple[str, int], ...]
    schema_sha256: str

    @property
    def counts(self) -> dict[str, int]:
        return dict(self.table_counts)


def inspect_live_database_shape(engine: Engine) -> DatabaseShape:
    """Read the current schema identity without mutating or migrating it."""

    raw_connection = engine.raw_connection()
    try:
        connection = _sqlite_connection(raw_connection)
        return _database_shape(connection)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot inspect live SQLite schema: {exc}") from exc
    finally:
        raw_connection.close()


def create_consistent_sqlite_backup(
    engine: Engine,
    destination: Path,
    *,
    expected_shape: DatabaseShape | None = None,
    overwrite: bool = False,
) -> SqliteSnapshotInfo:
    """Publish a validated online backup at ``destination`` atomically.

    ``destination`` must already have a parent directory.  Callers should put it
    below an application-owned backup root; this low-level helper deliberately
    does not invent or expand user paths.
    """

    target = Path(destination)
    if target.suffix.casefold() not in {".sqlite", ".sqlite3", ".db"}:
        raise BackupError("SQLite backup destination must use .sqlite, .sqlite3, or .db")
    if not target.parent.is_dir():
        raise BackupError("SQLite backup destination parent does not exist")
    if target.is_symlink():
        raise BackupError("refusing to replace a symbolic-link backup destination")
    if target.exists() and not overwrite:
        raise BackupError("backup destination already exists")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}-",
        suffix=".tmp.sqlite3",
        dir=target.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        _online_backup(engine, temporary)
        _flush_file(temporary)
        info = inspect_sqlite_snapshot(
            temporary,
            expected_shape=expected_shape,
        )
        if target.exists() and not overwrite:
            raise BackupError("backup destination appeared while snapshotting")
        os.replace(temporary, target)
        return SqliteSnapshotInfo(
            path=target,
            size_bytes=info.size_bytes,
            sha256=info.sha256,
            schema_revision=info.schema_revision,
            table_counts=info.table_counts,
            schema_sha256=info.schema_sha256,
        )
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"cannot create SQLite backup: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def inspect_sqlite_snapshot(
    path: Path,
    *,
    expected_shape: DatabaseShape | None = None,
    expected_counts: dict[str, int] | None = None,
) -> SqliteSnapshotInfo:
    """Fully validate an offline SQLite snapshot without modifying it."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise BackupError("SQLite snapshot must be a regular, non-symlink file")
    size_bytes = source.stat().st_size
    try:
        with source.open("rb") as stream:
            header = stream.read(16)
    except OSError as exc:
        raise BackupError(f"cannot read SQLite snapshot: {exc}") from exc
    if size_bytes < 100 or header != b"SQLite format 3\x00":
        raise BackupError("snapshot does not contain a SQLite database header")

    uri = f"{source.resolve().as_uri()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        quick_check = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
        if quick_check != ("ok",):
            raise BackupError(f"SQLite quick_check failed: {'; '.join(quick_check)}")
        violations = tuple(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise BackupError(f"SQLite foreign key violations: {violations!r}")
        shape = _database_shape(connection)
        if expected_shape is not None and shape != expected_shape:
            raise BackupError("snapshot schema does not match the installed application schema")
        counts = _table_counts(connection, shape.table_names)
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError(f"cannot validate SQLite snapshot: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    if expected_counts is not None and counts != expected_counts:
        raise BackupError("snapshot table counts do not match its export manifest")
    return SqliteSnapshotInfo(
        path=source,
        size_bytes=size_bytes,
        sha256=_file_sha256(source),
        schema_revision=shape.schema_revision,
        table_counts=tuple(sorted(counts.items())),
        schema_sha256=shape.schema_sha256,
    )


def _online_backup(engine: Engine, destination: Path) -> None:
    raw_connection = engine.raw_connection()
    target_connection = sqlite3.connect(destination)
    try:
        source_connection = _sqlite_connection(raw_connection)
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        raw_connection.close()


def _sqlite_connection(raw_connection: Any) -> sqlite3.Connection:
    driver = getattr(raw_connection, "driver_connection", None)
    if not isinstance(driver, sqlite3.Connection):
        driver = getattr(raw_connection, "connection", None)
    if not isinstance(driver, sqlite3.Connection):
        raise BackupError("SQLAlchemy engine is not backed by sqlite3")
    return driver


def _database_shape(connection: sqlite3.Connection) -> DatabaseShape:
    table_names = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    if "schema_version" not in table_names:
        raise BackupError("SQLite snapshot has no schema_version table")
    revision_rows = connection.execute("SELECT version_num FROM schema_version").fetchall()
    if (
        len(revision_rows) != 1
        or not isinstance(revision_rows[0][0], str)
        or not revision_rows[0][0]
    ):
        raise BackupError("SQLite snapshot must contain exactly one schema revision")
    schema_revision = str(revision_rows[0][0])

    forbidden_objects = tuple(
        connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('view', 'trigger') ORDER BY type, name"
        )
    )
    if forbidden_objects:
        raise BackupError("SQLite snapshot contains unsupported views or triggers")

    schema_description: dict[str, object] = {}
    for table_name in table_names:
        quoted = _quote_identifier(table_name)
        columns = [tuple(row) for row in connection.execute(f"PRAGMA table_info({quoted})")]
        foreign_key_groups: dict[int, list[tuple[Any, ...]]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({quoted})"):
            foreign_key_groups.setdefault(int(row[0]), []).append(tuple(row))
        foreign_keys = sorted(
            tuple(tuple(row[2:]) for row in sorted(group, key=lambda item: int(item[1])))
            for group in foreign_key_groups.values()
        )
        indexes: list[tuple[object, ...]] = []
        for row in connection.execute(f"PRAGMA index_list({quoted})"):
            index_name = str(row[1])
            index_quoted = _quote_identifier(index_name)
            index_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            indexes.append(
                (
                    index_name,
                    int(row[2]),
                    str(row[3]),
                    int(row[4]),
                    None if index_sql_row is None else index_sql_row[0],
                    tuple(
                        tuple(value[1:])
                        for value in sorted(
                            connection.execute(f"PRAGMA index_info({index_quoted})"),
                            key=lambda item: int(item[0]),
                        )
                    ),
                )
            )
        schema_description[table_name] = {
            "columns": columns,
            "constraints": _table_constraints(connection, table_name),
            "foreign_keys": foreign_keys,
            "indexes": sorted(indexes, key=lambda item: str(item[0])),
        }
    canonical = json.dumps(
        schema_description,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return DatabaseShape(
        schema_revision=schema_revision,
        table_names=table_names,
        schema_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _table_constraints(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise BackupError(f"SQLite snapshot has no definition for table {table_name!r}")
    sql = row[0]
    start = sql.find("(")
    end = sql.rfind(")")
    if start < 0 or end <= start:
        raise BackupError(f"SQLite table definition is malformed for {table_name!r}")
    constraint_prefixes = ("CONSTRAINT ", "PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")
    constraints = []
    for item in _split_top_level_sql(sql[start + 1 : end]):
        normalized = re.sub(r"\s+", " ", item.strip()).rstrip(",")
        if normalized.upper().startswith(constraint_prefixes):
            constraints.append(normalized)
    return tuple(sorted(constraints))


def _split_top_level_sql(value: str) -> tuple[str, ...]:
    items: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        current.append(character)
        if quote is not None:
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                    current.append(value[index])
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
        elif character == "[":
            quote = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            current.pop()
            items.append("".join(current))
            current = []
        index += 1
    if quote is not None or depth != 0:
        raise BackupError("SQLite table definition contains unbalanced SQL")
    if current:
        items.append("".join(current))
    return tuple(items)


def _table_counts(connection: sqlite3.Connection, table_names: tuple[str, ...]) -> dict[str, int]:
    return {
        table_name: int(
            connection.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}").fetchone()[
                0
            ]
        )
        for table_name in table_names
    }


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _flush_file(path: Path) -> None:
    # Windows requires a writable handle for ``FlushFileBuffers`` (used by
    # ``os.fsync``); reopening does not alter the already-written bytes.
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


__all__ = [
    "BackupError",
    "DatabaseShape",
    "SqliteSnapshotInfo",
    "create_consistent_sqlite_backup",
    "inspect_live_database_shape",
    "inspect_sqlite_snapshot",
]
