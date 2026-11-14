"""Hardlink-or-copy rotation and mtime retention. Runs only after the checkpoint is released."""

import logging
import os
import shutil
import stat
from datetime import datetime, timedelta
from pathlib import Path

from .config import BACKUP_BUCKETS, LOCK_NAME, Config

logger = logging.getLogger(__name__)


def last_name(prefix: str, when: datetime) -> str:
    return f"{prefix}-{when.strftime('%Y%m%d-%H%M%S')}.tar.zst"


def daily_name(prefix: str, when: datetime) -> str:
    return f"{prefix}-{when.strftime('%Y%m%d')}.tar.zst"


def weekly_name(prefix: str, when: datetime) -> str:
    # %G is the ISO year, so a week that crosses 1 January stays with that week.
    return f"{prefix}-{when.strftime('%G%V')}.tar.zst"


def monthly_name(prefix: str, when: datetime) -> str:
    return f"{prefix}-{when.strftime('%Y%m')}.tar.zst"


def latest_name(prefix: str) -> str:
    return f"{prefix}-latest.tar.zst"


def link_or_copy(src: Path, dest: Path) -> None:
    """Point ``dest`` at ``src``. Hardlink first. Copy if the link fails. Replace ``dest`` if it exists."""
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.unlink(missing_ok=True)
        try:
            os.link(src, tmp)
        except OSError as exc:
            logger.warning("hardlink failed (%s); copying %s", exc.strerror or exc, dest.name)
            shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def rotate(config: Config, last_archive: Path, when: datetime) -> None:
    """Link or copy the finished archive into the day, week, and month buckets, then publish latest."""
    buckets = {
        "daily": config.backup_dir / "daily" / daily_name(config.prefix, when),
        "weekly": config.backup_dir / "weekly" / weekly_name(config.prefix, when),
        "monthly": config.backup_dir / "monthly" / monthly_name(config.prefix, when),
    }
    for dest in buckets.values():
        link_or_copy(last_archive, dest)
    if config.latest_type == "none":
        return
    published = {
        "last": last_archive,
        "daily": buckets["daily"],
        "weekly": buckets["weekly"],
        "monthly": buckets["monthly"],
    }
    pointer_name = latest_name(config.prefix)
    for archive in published.values():
        pointer = archive.parent / pointer_name
        if config.latest_type == "symlink":
            _replace_symlink(pointer, archive.name)
        elif config.latest_type == "hardlink":
            link_or_copy(archive, pointer)
        else:
            raise ValueError(f"unknown latest type: {config.latest_type}")


def prune(config: Config, when: datetime, *, protect: set[str]) -> None:
    """Delete old ``{prefix}-*.tar.zst`` files. ``protect`` names are kept even if their mtime is old."""
    windows = {
        "last": timedelta(minutes=config.keep_mins),
        "daily": timedelta(days=config.keep_days),
        "weekly": timedelta(days=(config.keep_weeks * 7) + 1),
        "monthly": timedelta(days=(config.keep_months * 31) + 1),
    }
    for bucket in BACKUP_BUCKETS:
        removed = _prune_directory(config.backup_dir / bucket, config.prefix, when - windows[bucket], protect)
        if removed:
            logger.info("pruned %s archive(s) from %s", removed, bucket)


def _replace_symlink(pointer: Path, target_name: str) -> None:
    tmp = pointer.with_name(pointer.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        tmp.symlink_to(target_name)
        os.replace(tmp, pointer)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _prune_directory(directory: Path, prefix: str, cutoff: datetime, protect: set[str]) -> int:
    pattern = f"{prefix}-"
    suffix = ".tar.zst"
    removed = 0
    with os.scandir(directory) as scan:
        for entry in scan:
            if entry.name in protect or entry.name == LOCK_NAME:
                continue
            if not entry.name.startswith(pattern) or not entry.name.endswith(suffix):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                continue
            mtime = datetime.fromtimestamp(info.st_mtime, tz=cutoff.tzinfo)
            if mtime >= cutoff:
                continue
            # Unlink the directory entry. A symlink is removed. Its target is not.
            os.unlink(entry.path)
            removed += 1
    return removed
