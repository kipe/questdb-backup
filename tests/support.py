"""Fakes and a config built on tmp directories. No sockets and no QuestDB."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from questdb_backup.config import Config

WHEN = datetime(2026, 3, 20, 14, 30, tzinfo=UTC)


def make_config(tmp_path: Path, **overrides: object) -> Config:
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir()
    backup_dir.mkdir()
    for name in ("last", "daily", "weekly", "monthly"):
        (backup_dir / name).mkdir()
    (data_dir / "db").write_bytes(b"rows")
    config = Config(
        host="questdb.example",
        client="http",
        port=9000,
        user="",
        password="",
        database="qdb",
        data_dir=data_dir,
        backup_dir=backup_dir,
        prefix="questdb",
        schedule="0 3 * * *",
        tz=ZoneInfo("UTC"),
        backup_on_start=True,
        keep_days=7,
        keep_weeks=4,
        keep_months=6,
        keep_mins=1440,
        latest_type="symlink",
        healthcheck_port=0,
        zstd_level=3,
        zstd_threads=1,
    )
    if not overrides:
        return config
    return replace(config, **overrides)


class FakeClient:
    """Records SQL. ``error_on`` raises each time that statement is executed."""

    def __init__(
        self,
        *,
        in_progress: bool = False,
        started_at: datetime | None = None,
        error: Exception | None = None,
        error_on: str | None = None,
    ) -> None:
        self._in_progress = in_progress
        self._started_at = started_at
        self._error = error
        self._error_on = error_on
        self.calls: list[str] = []
        self.on_execute: Callable[[str], None] | None = None

    def checkpoint_status(self) -> tuple[bool, datetime | None]:
        return self._in_progress, self._started_at

    def execute(self, sql: str) -> None:
        if self.on_execute is not None:
            self.on_execute(sql)
        self.calls.append(sql)
        if self._error is not None and sql == self._error_on:
            raise self._error


class FakeRunner:
    """Writes a few bytes. Optionally raises after the partial file exists."""

    def __init__(self, *, payload: bytes = b"archive-bytes", error: BaseException | None = None) -> None:
        self.payload = payload
        self.error = error
        self.destinations: list[Path] = []

    def stream(self, data_dir: Path, destination: Path) -> None:
        self.destinations.append(destination)
        destination.write_bytes(self.payload)
        if self.error is not None:
            raise self.error

    def cancel(self) -> None:
        return
