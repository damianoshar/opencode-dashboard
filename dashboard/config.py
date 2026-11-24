"""Configuration and path resolution for the OpenCode dashboard.

Everything is resolved from the OpenCode data directory so the dashboard works
on any machine without configuration. Environment variables can override
everything for tests or for pointing at a copy of the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    return Path.home()


def default_opencode_dir() -> Path:
    env = os.environ.get("OPENCODE_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else _home() / ".local" / "share"
    return base / "opencode"


def default_state_dir() -> Path:
    env = os.environ.get("OPENCODE_STATE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else _home() / ".local" / "state"
    return base / "opencode"


@dataclass
class Settings:
    """Runtime settings for the dashboard."""

    opencode_dir: Path = field(default_factory=default_opencode_dir)
    state_dir: Path = field(default_factory=default_state_dir)
    db_path: Path | None = None
    project_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent)
    data_dir: Path | None = None
    recovery_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8791
    read_only: bool = False

    def __post_init__(self) -> None:
        self.opencode_dir = Path(self.opencode_dir)
        self.state_dir = Path(self.state_dir)
        if self.db_path is None:
            self.db_path = self.opencode_dir / "opencode.db"
        self.db_path = Path(self.db_path)
        if self.data_dir is None:
            self.data_dir = self.project_dir / "data"
        self.data_dir = Path(self.data_dir)
        if self.recovery_dir is None:
            self.recovery_dir = _home() / "opencode-recovery"
        self.recovery_dir = Path(self.recovery_dir)

    # ---- derived locations -------------------------------------------------
    @property
    def backup_dir(self) -> Path:
        """Where the dashboard writes its own backups."""
        return self.data_dir / "backups"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def bundle_dir(self) -> Path:
        return self.data_dir / "bundles"

    @property
    def audit_log(self) -> Path:
        return self.data_dir / "audit.log"

    @property
    def log_dir(self) -> Path:
        return self.opencode_dir / "log"

    @property
    def storage_dir(self) -> Path:
        return self.opencode_dir / "storage"

    @property
    def snapshot_dir(self) -> Path:
        return self.opencode_dir / "snapshot"

    @property
    def worktree_dir(self) -> Path:
        return self.opencode_dir / "worktree"

    @property
    def repos_dir(self) -> Path:
        return self.opencode_dir / "repos"

    @property
    def tool_output_dir(self) -> Path:
        return self.opencode_dir / "tool-output"

    @property
    def service_file(self) -> Path:
        return self.state_dir / "service.json"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.backup_dir, self.export_dir, self.bundle_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    env = os.environ.get
    settings = Settings(
        opencode_dir=Path(env("OPENCODE_DATA_DIR") or default_opencode_dir()),
        state_dir=Path(env("OPENCODE_STATE_DIR") or default_state_dir()),
    )
    if env("OPENCODE_DB"):
        settings.db_path = Path(env("OPENCODE_DB"))
    if env("DASHBOARD_DATA_DIR"):
        settings.data_dir = Path(env("DASHBOARD_DATA_DIR"))
    if env("DASHBOARD_RECOVERY_DIR"):
        settings.recovery_dir = Path(env("DASHBOARD_RECOVERY_DIR"))
    if env("DASHBOARD_HOST"):
        settings.host = env("DASHBOARD_HOST")
    if env("DASHBOARD_PORT"):
        settings.port = int(env("DASHBOARD_PORT"))
    if env("DASHBOARD_READ_ONLY"):
        settings.read_only = env("DASHBOARD_READ_ONLY").lower() in ("1", "true", "yes")
    return settings


SETTINGS = load_settings()
