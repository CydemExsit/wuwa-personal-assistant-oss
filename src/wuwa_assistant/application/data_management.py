"""Offline backup, export/import staging, and diagnostic-bundle contracts.

Restore is intentionally two-phase.  This service validates an export and
atomically writes ``pending-restore.sqlite3`` but never replaces the database
used by its open SQLAlchemy engine.  Startup code may activate that staged file
only after every database connection has been closed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from wuwa_assistant import __version__
from wuwa_assistant.storage.backup import (
    BackupError,
    SqliteSnapshotInfo,
    create_consistent_sqlite_backup,
    inspect_live_database_shape,
    inspect_sqlite_snapshot,
)
from wuwa_assistant.storage.health import inspect_database_health
from wuwa_assistant.storage.paths import AppPaths

EXPORT_FORMAT = "wuwa-personal-assistant-json-backup"
EXPORT_FORMAT_VERSION = 1
MAX_EXPORT_DOCUMENT_BYTES = 2 * 1024 * 1024 * 1024
MAX_DIAGNOSTIC_LOG_BYTES = 2 * 1024 * 1024
_DIAGNOSTIC_CATEGORIES = frozenset({"health", "version", "logs"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


class DataManagementError(RuntimeError):
    """Friendly failure for local backup/export/diagnostic operations."""


class ExportValidationError(DataManagementError):
    """Raised when an import is incomplete, tampered with, or incompatible."""


@dataclass(frozen=True, slots=True)
class ExportManifest:
    format_name: str
    format_version: int
    app_version: str
    schema_revision: str
    created_at: str
    table_counts: tuple[tuple[str, int], ...]
    database_size_bytes: int
    database_sha256: str
    schema_sha256: str

    @property
    def counts(self) -> dict[str, int]:
        return dict(self.table_counts)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "format": self.format_name,
            "format_version": self.format_version,
            "app_version": self.app_version,
            "schema_revision": self.schema_revision,
            "created_at": self.created_at,
            "table_counts": self.counts,
            "database_size_bytes": self.database_size_bytes,
            "database_sha256": self.database_sha256,
            "schema_sha256": self.schema_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    path: Path
    manifest: ExportManifest
    document_sha256: str


@dataclass(frozen=True, slots=True)
class ImportPreview:
    source_path: Path
    manifest: ExportManifest
    document_sha256: str
    restart_required: bool = True


@dataclass(frozen=True, slots=True)
class StagedRestore:
    staged_path: Path
    manifest: ExportManifest
    source_document_sha256: str
    database_sha256: str
    restart_required: bool = True


@dataclass(frozen=True, slots=True)
class DiagnosticEntry:
    category: str
    archive_name: str
    size_bytes: int
    sha256: str
    description: str


@dataclass(frozen=True, slots=True)
class DiagnosticPreview:
    categories: tuple[str, ...]
    entries: tuple[DiagnosticEntry, ...]
    confirmation_token: str
    excluded_by_design: tuple[str, ...] = (
        "database",
        "full-frame",
        "screenshots",
        "debug-images",
        "roi-images",
    )


@dataclass(frozen=True, slots=True)
class DiagnosticBundleReceipt:
    path: Path
    entries: tuple[DiagnosticEntry, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedExport:
    preview: ImportPreview
    database_bytes: bytes


class DataManagementService:
    """Application-owned, path-bounded local data protection service."""

    def __init__(
        self,
        *,
        engine: Engine,
        paths: AppPaths,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        app_version: str = __version__,
    ) -> None:
        self._engine = engine
        self._paths = paths
        self._clock = clock
        self._app_version = _nonempty_text(app_version, "app_version")
        self._paths.ensure_created()
        self._shape = inspect_live_database_shape(engine)

    @property
    def pending_restore_path(self) -> Path:
        return self._paths.restore_staging_dir / "pending-restore.sqlite3"

    def create_sqlite_backup(self, filename: str | None = None) -> SqliteSnapshotInfo:
        """Create a consistent backup below the app-owned backups directory."""

        backup_dir = self._paths.backups_dir / "database"
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_leaf(
            filename or f"wuwa-backup-{self._timestamp_slug()}.sqlite3",
            suffix=".sqlite3",
        )
        try:
            return create_consistent_sqlite_backup(
                self._engine,
                backup_dir / safe_name,
                expected_shape=self._shape,
            )
        except BackupError as exc:
            raise DataManagementError(str(exc)) from exc

    def export_json(self, filename: str | None = None) -> ExportReceipt:
        """Export one validated SQLite snapshot in a versioned JSON envelope."""

        export_dir = self._paths.exports_dir
        work_dir = self._paths.backups_dir / ".snapshot-work"
        export_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_leaf(
            filename or f"wuwa-export-{self._timestamp_slug()}.json",
            suffix=".json",
        )
        snapshot_path = work_dir / f"snapshot-{os.urandom(8).hex()}.sqlite3"
        try:
            snapshot = create_consistent_sqlite_backup(
                self._engine,
                snapshot_path,
                expected_shape=self._shape,
            )
            database_bytes = snapshot_path.read_bytes()
            manifest = ExportManifest(
                format_name=EXPORT_FORMAT,
                format_version=EXPORT_FORMAT_VERSION,
                app_version=self._app_version,
                schema_revision=snapshot.schema_revision,
                created_at=self._timestamp(),
                table_counts=snapshot.table_counts,
                database_size_bytes=snapshot.size_bytes,
                database_sha256=snapshot.sha256,
                schema_sha256=snapshot.schema_sha256,
            )
            document = _canonical_json_bytes(
                {
                    "manifest": manifest.to_json_dict(),
                    "database_b64": base64.b64encode(database_bytes).decode("ascii"),
                }
            )
            destination = export_dir / safe_name
            _atomic_write_new(destination, document)
            return ExportReceipt(
                path=destination,
                manifest=manifest,
                document_sha256=hashlib.sha256(document).hexdigest(),
            )
        except BackupError as exc:
            raise DataManagementError(str(exc)) from exc
        except OSError as exc:
            raise DataManagementError(f"cannot export JSON backup: {exc}") from exc
        finally:
            snapshot_path.unlink(missing_ok=True)

    def preview_import(self, source_path: str | Path) -> ImportPreview:
        """Fully validate an export without writing to the live or staged DB."""

        return self._load_and_validate_export(source_path).preview

    def stage_import(
        self,
        source_path: str | Path,
        *,
        expected_document_sha256: str,
        replace_existing_stage: bool = False,
    ) -> StagedRestore:
        """Atomically stage a revalidated snapshot for activation after restart.

        The currently open database is never replaced.  ``expected_document_sha256``
        must come from the preview shown to the user, preventing a changed file from
        being staged after confirmation.
        """

        expected_hash = _sha256_text(expected_document_sha256, "expected document SHA-256")
        validated = self._load_and_validate_export(source_path)
        if validated.preview.document_sha256 != expected_hash:
            raise ExportValidationError("import file changed after preview")

        destination = self.pending_restore_path
        if destination.is_symlink():
            raise ExportValidationError("pending restore path is a symbolic link")
        if destination.exists() and not replace_existing_stage:
            raise ExportValidationError("a pending restore already exists")
        try:
            temporary = _write_temporary_bytes(
                destination.parent,
                prefix=".pending-restore-",
                suffix=".tmp.sqlite3",
                content=validated.database_bytes,
            )
        except OSError as exc:
            raise ExportValidationError(f"cannot create pending restore: {exc}") from exc
        try:
            inspect_sqlite_snapshot(
                temporary,
                expected_shape=self._shape,
                expected_counts=validated.preview.manifest.counts,
            )
            if destination.exists() and not replace_existing_stage:
                raise ExportValidationError("a pending restore appeared while staging")
            os.replace(temporary, destination)
        except BackupError as exc:
            raise ExportValidationError(str(exc)) from exc
        except OSError as exc:
            raise ExportValidationError(f"cannot publish pending restore: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return StagedRestore(
            staged_path=destination,
            manifest=validated.preview.manifest,
            source_document_sha256=validated.preview.document_sha256,
            database_sha256=validated.preview.manifest.database_sha256,
        )

    def preview_diagnostic_bundle(
        self,
        categories: Iterable[str] = (),
    ) -> DiagnosticPreview:
        """List the exact allowlisted files that would enter a diagnostic ZIP."""

        selected = _diagnostic_categories(categories)
        payloads = self._diagnostic_payloads(selected)
        return _diagnostic_preview(selected, payloads)

    def create_diagnostic_bundle(
        self,
        preview: DiagnosticPreview,
        *,
        confirmed_categories: Iterable[str],
        filename: str | None = None,
    ) -> DiagnosticBundleReceipt:
        """Create a ZIP only after exact-category confirmation of a fresh preview."""

        if not isinstance(preview, DiagnosticPreview):
            raise DataManagementError("diagnostic preview is invalid")
        confirmed = _diagnostic_categories(confirmed_categories)
        if not confirmed or confirmed != preview.categories:
            raise DataManagementError("diagnostic categories were not explicitly confirmed")
        payloads = self._diagnostic_payloads(confirmed)
        current = _diagnostic_preview(confirmed, payloads)
        if current.confirmation_token != preview.confirmation_token:
            raise DataManagementError("diagnostic contents changed after preview")

        diagnostic_dir = self._paths.diagnostics_dir
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_leaf(
            filename or f"wuwa-diagnostics-{self._timestamp_slug()}.zip",
            suffix=".zip",
        )
        destination = diagnostic_dir / safe_name
        if destination.exists() or destination.is_symlink():
            raise DataManagementError("diagnostic destination already exists")
        manifest_bytes = _canonical_json_bytes(
            {
                "format": "wuwa-personal-assistant-diagnostics",
                "format_version": 1,
                "created_at": self._timestamp(),
                "confirmed_categories": list(confirmed),
                "entries": [
                    {
                        "category": entry.category,
                        "archive_name": entry.archive_name,
                        "size_bytes": entry.size_bytes,
                        "sha256": entry.sha256,
                    }
                    for entry in current.entries
                ],
                "excluded_by_design": list(current.excluded_by_design),
            }
        )
        temporary = _temporary_path(
            destination.parent,
            prefix=f".{destination.stem}-",
            suffix=".tmp.zip",
        )
        try:
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.writestr("diagnostic-manifest.json", manifest_bytes)
                for _category, archive_name, _description, content in payloads:
                    archive.writestr(archive_name, content)
            _flush_file(temporary)
            os.replace(temporary, destination)
        except (OSError, zipfile.BadZipFile) as exc:
            raise DataManagementError(f"cannot create diagnostic ZIP: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return DiagnosticBundleReceipt(
            path=destination,
            entries=current.entries,
            sha256=_file_sha256(destination),
        )

    def _load_and_validate_export(self, source_path: str | Path) -> _ValidatedExport:
        source = _import_source(source_path)
        try:
            size = source.stat().st_size
            if size <= 0 or size > MAX_EXPORT_DOCUMENT_BYTES:
                raise ExportValidationError("import document size is invalid")
            document = source.read_bytes()
            root = json.loads(document)
        except ExportValidationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExportValidationError(f"cannot read import JSON: {exc}") from exc
        if not isinstance(root, dict) or set(root) != {"manifest", "database_b64"}:
            raise ExportValidationError("import JSON is incomplete or has unknown top-level fields")
        manifest = _parse_manifest(root.get("manifest"))
        if manifest.schema_revision != self._shape.schema_revision:
            raise ExportValidationError("backup schema revision does not match this application")
        if manifest.schema_sha256 != self._shape.schema_sha256:
            raise ExportValidationError("backup schema fingerprint does not match this application")
        encoded = root.get("database_b64")
        if not isinstance(encoded, str) or not encoded:
            raise ExportValidationError("database_b64 must be nonempty text")
        try:
            database_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ExportValidationError("database_b64 is not valid base64") from exc
        if len(database_bytes) != manifest.database_size_bytes:
            raise ExportValidationError("database size does not match the export manifest")
        if hashlib.sha256(database_bytes).hexdigest() != manifest.database_sha256:
            raise ExportValidationError("database SHA-256 does not match the export manifest")

        validation_dir = self._paths.backups_dir / ".import-validation"
        try:
            validation_dir.mkdir(parents=True, exist_ok=True)
            temporary = _write_temporary_bytes(
                validation_dir,
                prefix="import-",
                suffix=".tmp.sqlite3",
                content=database_bytes,
            )
        except OSError as exc:
            raise ExportValidationError(f"cannot create import validation file: {exc}") from exc
        try:
            inspect_sqlite_snapshot(
                temporary,
                expected_shape=self._shape,
                expected_counts=manifest.counts,
            )
        except BackupError as exc:
            raise ExportValidationError(str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return _ValidatedExport(
            preview=ImportPreview(
                source_path=source,
                manifest=manifest,
                document_sha256=hashlib.sha256(document).hexdigest(),
            ),
            database_bytes=database_bytes,
        )

    def _diagnostic_payloads(
        self,
        categories: tuple[str, ...],
    ) -> tuple[tuple[str, str, str, bytes], ...]:
        payloads: list[tuple[str, str, str, bytes]] = []
        if "version" in categories:
            payloads.append(
                (
                    "version",
                    "version.json",
                    "Application and export format versions",
                    _canonical_json_bytes(
                        {
                            "app_version": self._app_version,
                            "export_format_version": EXPORT_FORMAT_VERSION,
                        }
                    ),
                )
            )
        if "health" in categories:
            health = inspect_database_health(
                self._engine,
                self._paths.database_path,
                require_schema=True,
            )
            payloads.append(
                (
                    "health",
                    "database-health.json",
                    "SQLite health flags without database contents or absolute paths",
                    _canonical_json_bytes(
                        {
                            "database_filename": self._paths.database_path.name,
                            "quick_check": health.quick_check,
                            "foreign_keys_enabled": health.foreign_keys_enabled,
                            "journal_mode": health.journal_mode,
                            "schema_revision": health.schema_revision,
                        }
                    ),
                )
            )
        if "logs" in categories:
            for log_path in sorted(
                self._paths.logs_dir.iterdir(),
                key=lambda item: item.name.casefold(),
            ):
                if (
                    log_path.is_symlink()
                    or not log_path.is_file()
                    or log_path.suffix.casefold() not in {".log", ".txt"}
                    or log_path.stat().st_size > MAX_DIAGNOSTIC_LOG_BYTES
                ):
                    continue
                content = log_path.read_bytes()
                if b"\x00" in content:
                    continue
                text_content = content.decode("utf-8", errors="replace").encode("utf-8")
                payloads.append(
                    (
                        "logs",
                        f"logs/{_safe_archive_leaf(log_path.name)}",
                        f"User-confirmed UTF-8 text log: {log_path.name}",
                        text_content,
                    )
                )
        return tuple(payloads)

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DataManagementError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _timestamp_slug(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DataManagementError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_manifest(value: object) -> ExportManifest:
    required = {
        "format",
        "format_version",
        "app_version",
        "schema_revision",
        "created_at",
        "table_counts",
        "database_size_bytes",
        "database_sha256",
        "schema_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ExportValidationError("export manifest is incomplete or has unknown fields")
    if value.get("format") != EXPORT_FORMAT:
        raise ExportValidationError("export format is not supported")
    if value.get("format_version") != EXPORT_FORMAT_VERSION:
        raise ExportValidationError("export format version is not supported")
    app_version = _manifest_text(value.get("app_version"), "app_version")
    schema_revision = _manifest_text(value.get("schema_revision"), "schema_revision")
    created_at = _manifest_timestamp(value.get("created_at"))
    counts_value = value.get("table_counts")
    if not isinstance(counts_value, Mapping) or not counts_value:
        raise ExportValidationError("table_counts must be a nonempty object")
    counts: list[tuple[str, int]] = []
    for table_name, count in counts_value.items():
        if not isinstance(table_name, str) or not table_name or type(count) is not int or count < 0:
            raise ExportValidationError("table_counts contains an invalid entry")
        counts.append((table_name, count))
    size = value.get("database_size_bytes")
    if type(size) is not int or size <= 0:
        raise ExportValidationError("database_size_bytes must be positive")
    return ExportManifest(
        format_name=EXPORT_FORMAT,
        format_version=EXPORT_FORMAT_VERSION,
        app_version=app_version,
        schema_revision=schema_revision,
        created_at=created_at,
        table_counts=tuple(sorted(counts)),
        database_size_bytes=size,
        database_sha256=_sha256_text(value.get("database_sha256"), "database SHA-256"),
        schema_sha256=_sha256_text(value.get("schema_sha256"), "schema SHA-256"),
    )


def _manifest_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ExportValidationError(f"manifest {label} is invalid")
    return value.strip()


def _manifest_timestamp(value: object) -> str:
    text = _manifest_text(value, "created_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportValidationError("manifest created_at is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ExportValidationError("manifest created_at must include a timezone")
    return text


def _sha256_text(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ExportValidationError(f"{label} is invalid")
    normalized = value.casefold()
    if any(character not in "0123456789abcdef" for character in normalized):
        raise ExportValidationError(f"{label} is invalid")
    return normalized


def _safe_leaf(value: str, *, suffix: str) -> str:
    if not isinstance(value, str):
        raise DataManagementError("output filename must be text")
    safe = value.strip()
    if (
        not safe
        or safe in {".", ".."}
        or "/" in safe
        or "\\" in safe
        or ":" in safe
        or "\x00" in safe
        or Path(safe).name != safe
        or not safe.casefold().endswith(suffix.casefold())
    ):
        raise DataManagementError(f"output filename must be one safe {suffix} leaf name")
    stem = safe.split(".", maxsplit=1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES or safe.endswith((" ", ".")):
        raise DataManagementError("output filename is reserved on Windows")
    return safe


def _safe_archive_leaf(value: str) -> str:
    if not value or "/" in value or "\\" in value or Path(value).name != value:
        raise DataManagementError("diagnostic log filename is unsafe")
    return value


def _import_source(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ExportValidationError("import source must not be a symbolic link")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ExportValidationError(f"import source does not exist: {exc}") from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".json":
        raise ExportValidationError("import source must be a regular .json file")
    return resolved


def _diagnostic_categories(values: Iterable[str]) -> tuple[str, ...]:
    try:
        selected = tuple(sorted(set(values)))
    except TypeError as exc:
        raise DataManagementError("diagnostic categories must be text") from exc
    if any(not isinstance(value, str) or value not in _DIAGNOSTIC_CATEGORIES for value in selected):
        raise DataManagementError("diagnostic category is not allowlisted")
    return selected


def _diagnostic_preview(
    categories: tuple[str, ...],
    payloads: tuple[tuple[str, str, str, bytes], ...],
) -> DiagnosticPreview:
    entries = tuple(
        DiagnosticEntry(
            category=category,
            archive_name=archive_name,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            description=description,
        )
        for category, archive_name, description, content in payloads
    )
    return DiagnosticPreview(
        categories=categories,
        entries=entries,
        confirmation_token=_diagnostic_token(categories, entries),
    )


def _diagnostic_token(
    categories: tuple[str, ...],
    entries: tuple[DiagnosticEntry, ...],
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "categories": list(categories),
                "entries": [
                    {
                        "category": entry.category,
                        "archive_name": entry.archive_name,
                        "size_bytes": entry.size_bytes,
                        "sha256": entry.sha256,
                    }
                    for entry in entries
                ],
            }
        )
    ).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write_new(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise DataManagementError("output destination already exists")
    temporary = _write_temporary_bytes(
        path.parent,
        prefix=f".{path.stem}-",
        suffix=f".tmp{path.suffix}",
        content=content,
    )
    try:
        if path.exists() or path.is_symlink():
            raise DataManagementError("output destination appeared while writing")
        os.replace(temporary, path)
    except OSError as exc:
        raise DataManagementError(f"cannot publish output file: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _write_temporary_bytes(
    directory: Path,
    *,
    prefix: str,
    suffix: str,
    content: bytes,
) -> Path:
    path = _temporary_path(directory, prefix=prefix, suffix=suffix)
    try:
        with path.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _temporary_path(directory: Path, *, prefix: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(descriptor)
    return Path(name)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _flush_file(path: Path) -> None:
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


__all__ = [
    "DataManagementError",
    "DataManagementService",
    "DiagnosticBundleReceipt",
    "DiagnosticEntry",
    "DiagnosticPreview",
    "ExportManifest",
    "ExportReceipt",
    "ExportValidationError",
    "ImportPreview",
    "StagedRestore",
]
