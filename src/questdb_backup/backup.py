"""One checkpoint backup. The SQL client, clock, and archive writer are injected."""

import logging
import os
import signal
import stat
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from compression import zstd
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Protocol
from zoneinfo import ZoneInfo

from .client import SqlClient
from .config import LOCK_NAME, Config
from .retention import daily_name, last_name, latest_name, monthly_name, prune, rotate, weekly_name

logger = logging.getLogger(__name__)

SQL_CREATE = "CHECKPOINT CREATE"
SQL_RELEASE = "CHECKPOINT RELEASE"

# A stop during a SQL call is not aborted. The lockfile is written first, so the next
# start can still release a checkpoint if the process is killed mid-request. The archive
# checks the stop flag between files, then the finally block releases the checkpoint.


class ArchiveError(Exception):
    """The zstd tar could not be written."""


class ProcessRunner(Protocol):
    def stream(self, data_dir: Path, destination: Path) -> None:
        """Write a zstd tar of ``data_dir`` to ``destination``. Raise on failure."""

    def cancel(self) -> None:
        """Stop an in-flight stream. Safe to call from a signal handler."""


class StopController:
    """Process-wide stop flag. The signal handler only sets a bool and asks the archive to stop."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self._requested = False
        self._runner: ProcessRunner | None = None
        self._previous: list[tuple[signal.Signals, signal.Handlers]] | None = None

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self, signum: int = signal.SIGTERM) -> None:
        if self.signum is None:
            self.signum = signum
        self._requested = True
        runner = self._runner
        if runner is not None:
            runner.cancel()

    def wait(self, seconds: float) -> bool:
        """Sleep until the deadline. Return True if a stop was requested."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.5, remaining))
        return True

    @contextmanager
    def bind(self, runner: ProcessRunner) -> Iterator[None]:
        self._runner = runner
        try:
            yield
        finally:
            if self._runner is runner:
                self._runner = None

    def install(self) -> None:
        if self._previous is not None or threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, frame: FrameType | None) -> None:
            self.request(signum)

        self._previous = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._previous.append((sig, signal.signal(sig, handler)))

    def restore(self) -> None:
        if not self._previous:
            return
        for sig, previous in self._previous:
            signal.signal(sig, previous)
        self._previous = None


class TarZstdRunner:
    """Write a zstd-compressed tar with the stdlib. Other filesystems are skipped."""

    def __init__(self, level: int, threads: int) -> None:
        self._level = level
        self._threads = threads
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def stream(self, data_dir: Path, destination: Path) -> None:
        self._stop = False
        root_dev = data_dir.stat().st_dev
        options = {
            zstd.CompressionParameter.compression_level: self._level,
            zstd.CompressionParameter.nb_workers: self._threads,
        }
        with (
            destination.open("wb") as raw,
            zstd.ZstdFile(raw, mode="wb", options=options) as compressed,
            tarfile.open(fileobj=compressed, mode="w|") as archive,
        ):
            self._add(archive, data_dir, ".", root_dev)

    def _add(self, archive: tarfile.TarFile, path: Path, arcname: str, root_dev: int) -> None:
        if self._stop:
            raise ArchiveError("archive stopped")
        info = archive.gettarinfo(os.fspath(path), arcname)
        if info is None:
            return
        # A mount inside the data directory is a different device. Skip it, and do not walk it.
        if arcname != "." and path.lstat().st_dev != root_dev:
            return
        if info.isreg():
            with path.open("rb") as source:
                archive.addfile(info, source)
        else:
            archive.addfile(info)
        if not info.isdir() or info.issym():
            return
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            child_arc = child.name if arcname == "." else f"{arcname}/{child.name}"
            self._add(archive, child, child_arc, root_dev)


def run_backup(
    config: Config,
    client: SqlClient,
    runner: ProcessRunner,
    *,
    now: Callable[[], datetime] | None = None,
    stop: StopController | None = None,
) -> bool:
    """Run one backup. Return True only after a renamed archive, rotation, and prune."""
    clock = now if now is not None else lambda: datetime.now(config.tz)
    token = stop if stop is not None else StopController()
    with token.bind(runner):
        try:
            return _run(config, client, runner, clock, token)
        except Exception:
            logger.exception("backup run failed")
            return False


def _run(
    config: Config,
    client: SqlClient,
    runner: ProcessRunner,
    now: Callable[[], datetime],
    stop: StopController,
) -> bool:
    lock_path = config.backup_dir / LOCK_NAME
    last_dir = config.backup_dir / "last"
    in_progress, started_at = client.checkpoint_status()
    lock_exists = _exists(lock_path)

    if in_progress and not lock_exists:
        logger.error(
            "foreign checkpoint is held since %s; not releasing it and not deleting archives",
            _format_started(started_at),
        )
        return False

    recovered = False
    if in_progress and lock_exists:
        logger.warning("lockfile found during an in-progress checkpoint; releasing it and removing partial archives")
        try:
            client.execute(SQL_RELEASE)
        except Exception:
            logger.exception("recovery CHECKPOINT RELEASE failed; keeping the lockfile and partial archives")
            return False
        _delete_partials(last_dir, config.prefix)
        recovered = True
    elif lock_exists:
        logger.warning("removing stale lockfile; checkpoint is not in progress")
        _delete_partials(last_dir, config.prefix)
        _remove(lock_path)

    if stop.requested:
        if recovered:
            _remove(lock_path)
        logger.info("stop requested before checkpoint")
        return False

    moment = _in_zone(now(), config.tz)
    final = last_dir / last_name(config.prefix, moment)
    partial = last_dir / f"{final.name}.partial"
    _write_lock(lock_path, moment)

    called_create = False
    tar_ok = False
    released = False
    try:
        if not stop.requested:
            called_create = True
            logger.info("CHECKPOINT CREATE")
            client.execute(SQL_CREATE)
            if not stop.requested:
                logger.info("archiving %s to %s", config.data_dir, partial.name)
                runner.stream(config.data_dir, partial)
                tar_ok = True
    except ArchiveError as exc:
        logger.error("%s", exc)
    except Exception:
        logger.exception("backup failed before a finished archive")
    finally:
        if called_create and not released:
            try:
                logger.info("CHECKPOINT RELEASE")
                client.execute(SQL_RELEASE)
                released = True
            except Exception:
                logger.exception("CHECKPOINT RELEASE failed")

    if called_create and not released:
        _remove(partial)
        logger.error("release failed; keeping the lockfile so the next start can recover")
        return False

    if not tar_ok:
        _remove(partial)
        _remove(lock_path)
        if stop.requested:
            logger.info("backup stopped")
        return False

    try:
        _fsync_file(partial)
        os.replace(partial, final)
        _fsync_dir(last_dir)
    except OSError:
        logger.exception("could not publish %s", final.name)
        _remove(partial)
        _remove(lock_path)
        return False

    try:
        rotate(config, final, moment)
        protect = {
            final.name,
            daily_name(config.prefix, moment),
            weekly_name(config.prefix, moment),
            monthly_name(config.prefix, moment),
        }
        if config.latest_type != "none":
            protect.add(latest_name(config.prefix))
        prune(config, moment, protect=protect)
    except Exception:
        logger.exception("rotation or prune failed; keeping %s", final.name)
        _remove(lock_path)
        return False

    _remove(lock_path)
    logger.info("backup finished: %s", final.name)
    return True


def _write_lock(path: Path, when: datetime) -> None:
    """Write and flush the lock before CHECKPOINT CREATE so a crash still looks like ours."""
    payload = f"pid={os.getpid()}\ntimestamp={when.isoformat()}\n".encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _delete_partials(directory: Path, prefix: str) -> None:
    suffix = ".tar.zst.partial"
    with os.scandir(directory) as scan:
        for entry in scan:
            if not entry.name.startswith(f"{prefix}-") or not entry.name.endswith(suffix):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                os.unlink(entry.path)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _in_zone(moment: datetime, tz: ZoneInfo) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _format_started(started_at: datetime | None) -> str:
    if started_at is None:
        return "unknown"
    return started_at.isoformat()
