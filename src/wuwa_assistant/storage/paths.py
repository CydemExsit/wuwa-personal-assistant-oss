"""Local application paths for user-owned data.

The repository is never used as an implicit data directory.  Tests and portable
launchers may inject an explicit root; normal installations use LocalAppData.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from wuwa_assistant.display import APP_ID


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved, separated directories for all persistent application data."""

    root: Path
    config_dir: Path
    data_dir: Path
    logs_dir: Path
    backups_dir: Path
    debug_dir: Path
    exports_dir: Path

    @classmethod
    def from_root(cls, root: str | Path) -> AppPaths:
        """Build paths below an explicit root without touching the filesystem."""

        resolved = Path(root).expanduser().resolve()
        return cls(
            root=resolved,
            config_dir=resolved / "config",
            data_dir=resolved / "data",
            logs_dir=resolved / "logs",
            backups_dir=resolved / "backups",
            debug_dir=resolved / "debug",
            exports_dir=resolved / "exports",
        )

    @classmethod
    def default(cls) -> AppPaths:
        """Return the normal per-user, non-roaming Windows application root."""

        root = user_data_path(
            appname=APP_ID,
            appauthor=False,
            roaming=False,
            ensure_exists=False,
        )
        return cls.from_root(root)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "wuwa-personal-assistant.sqlite3"

    @property
    def bootstrap_path(self) -> Path:
        """Small locator/config file that may be read before opening the database."""

        return self.config_dir / "bootstrap.json"

    @property
    def low_confidence_debug_dir(self) -> Path:
        return self.debug_dir / "low-confidence-rois"

    @property
    def corruption_archive_dir(self) -> Path:
        return self.backups_dir / "corrupt"

    @property
    def diagnostics_dir(self) -> Path:
        return self.exports_dir / "diagnostics"

    @property
    def public_data_cache_dir(self) -> Path:
        """Processed official public notices; never account or game-capture data."""

        return self.root / "cache" / "public-data"

    @property
    def restore_staging_dir(self) -> Path:
        return self.backups_dir / "restore-staging"

    def ensure_created(self) -> None:
        """Create only application-owned directories, never a database file."""

        for directory in (
            self.config_dir,
            self.data_dir,
            self.logs_dir,
            self.backups_dir,
            self.exports_dir,
            self.low_confidence_debug_dir,
            self.corruption_archive_dir,
            self.diagnostics_dir,
            self.restore_staging_dir,
            self.public_data_cache_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
