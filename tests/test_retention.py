"""Hardlink fallback, latest pointers, filenames, and mtime prune rules."""

import errno
import os
from datetime import datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from questdb_backup.config import Config
from questdb_backup.retention import (
    daily_name,
    last_name,
    link_or_copy,
    monthly_name,
    prune,
    rotate,
    weekly_name,
)
from tests.support import WHEN, make_config

PAYLOAD = b"archive-bytes"


def test_archive_filenames() -> None:
    # 1 Jan 2021 is ISO week 53 of 2020, so the week file uses %G, not the calendar year.
    when = datetime(2021, 1, 1, 3, 4, 5, tzinfo=WHEN.tzinfo)
    assert last_name("questdb", when) == "questdb-20210101-030405.tar.zst"
    assert daily_name("questdb", when) == "questdb-20210101.tar.zst"
    assert weekly_name("questdb", when) == "questdb-202053.tar.zst"
    assert monthly_name("questdb", when) == "questdb-202101.tar.zst"


def test_link_or_copy_copies_when_hardlink_fails(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    src = tmp_path / "src.tar.zst"
    dest = tmp_path / "bucket" / "questdb-20260320.tar.zst"
    dest.parent.mkdir()
    src.write_bytes(PAYLOAD)

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("questdb_backup.retention.os.link", fail_link)
    link_or_copy(src, dest)

    assert dest.read_bytes() == PAYLOAD
    assert dest.stat().st_nlink == 1
    assert not dest.samefile(src)


def test_latest_symlink_points_at_the_sibling_name(tmp_path: Path) -> None:
    config = make_config(tmp_path, latest_type="symlink")
    archive = _finished_archive(config)
    rotate(config, archive, WHEN)

    expected = {
        "last": last_name(config.prefix, WHEN),
        "daily": daily_name(config.prefix, WHEN),
        "weekly": weekly_name(config.prefix, WHEN),
        "monthly": monthly_name(config.prefix, WHEN),
    }
    for bucket, name in expected.items():
        pointer = config.backup_dir / bucket / "questdb-latest.tar.zst"
        target = os.readlink(pointer)
        assert target == name
        assert not Path(target).is_absolute()


def test_latest_none_creates_no_pointer(tmp_path: Path) -> None:
    config = make_config(tmp_path, latest_type="none")
    rotate(config, _finished_archive(config), WHEN)

    for bucket in ("last", "daily", "weekly", "monthly"):
        assert not os.path.lexists(config.backup_dir / bucket / "questdb-latest.tar.zst")
    assert (config.backup_dir / "daily" / daily_name(config.prefix, WHEN)).read_bytes() == PAYLOAD


def test_latest_hardlink_links_the_archive(tmp_path: Path) -> None:
    config = make_config(tmp_path, latest_type="hardlink")
    archive = _finished_archive(config)
    rotate(config, archive, WHEN)

    for bucket in ("last", "daily", "weekly", "monthly"):
        pointer = config.backup_dir / bucket / "questdb-latest.tar.zst"
        assert not pointer.is_symlink()
        assert pointer.samefile(archive)
        assert pointer.stat().st_nlink >= 2


def test_latest_hardlink_copies_when_link_fails(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("questdb_backup.retention.os.link", fail_link)
    config = make_config(tmp_path, latest_type="hardlink")
    archive = _finished_archive(config)
    rotate(config, archive, WHEN)

    pointer = config.backup_dir / "last" / "questdb-latest.tar.zst"
    assert not pointer.is_symlink()
    assert pointer.read_bytes() == PAYLOAD
    assert pointer.stat().st_nlink == 1
    assert not pointer.samefile(archive)


def test_prune_deletes_archives_older_than_each_window(tmp_path: Path) -> None:
    config = _retention_config(tmp_path)
    for bucket, window in _windows(config).items():
        directory = config.backup_dir / bucket
        old = directory / "questdb-old.tar.zst"
        fresh = directory / "questdb-fresh.tar.zst"
        old.write_bytes(b"old")
        fresh.write_bytes(b"fresh")
        _set_mtime(old, WHEN - window - timedelta(seconds=1))
        _set_mtime(fresh, WHEN)

    prune(config, WHEN, protect=set())

    for bucket in _windows(config):
        directory = config.backup_dir / bucket
        assert not (directory / "questdb-old.tar.zst").exists()
        assert (directory / "questdb-fresh.tar.zst").read_bytes() == b"fresh"


def test_prune_keeps_a_file_exactly_at_the_cutoff(tmp_path: Path) -> None:
    config = _retention_config(tmp_path)
    for bucket, window in _windows(config).items():
        directory = config.backup_dir / bucket
        edge = directory / "questdb-edge.tar.zst"
        older = directory / "questdb-older.tar.zst"
        edge.write_bytes(b"edge")
        older.write_bytes(b"older")
        _set_mtime(edge, WHEN - window)
        _set_mtime(older, WHEN - window - timedelta(seconds=1))

    prune(config, WHEN, protect=set())

    for bucket in _windows(config):
        directory = config.backup_dir / bucket
        assert (directory / "questdb-edge.tar.zst").read_bytes() == b"edge"
        assert not (directory / "questdb-older.tar.zst").exists()


def test_prune_keeps_the_archive_just_written_even_if_old(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    name = last_name(config.prefix, WHEN)
    archive = config.backup_dir / "last" / name
    other = config.backup_dir / "last" / "questdb-old.tar.zst"
    archive.write_bytes(PAYLOAD)
    other.write_bytes(b"old")
    _set_mtime(archive, WHEN - timedelta(days=1000))
    _set_mtime(other, WHEN - timedelta(days=1000))

    prune(config, WHEN, protect={name})

    assert archive.read_bytes() == PAYLOAD
    assert not other.exists()


def test_prune_never_deletes_the_checkpoint_lock(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    lock = config.backup_dir / "last" / ".checkpoint-lock"
    root_lock = config.backup_dir / ".checkpoint-lock"
    old = config.backup_dir / "last" / "questdb-old.tar.zst"
    lock.write_text("pid=1\n")
    root_lock.write_text("pid=1\n")
    old.write_bytes(b"old")
    _set_mtime(lock, WHEN - timedelta(days=1000))
    _set_mtime(old, WHEN - timedelta(days=1000))

    prune(config, WHEN, protect=set())

    assert lock.read_text() == "pid=1\n"
    assert root_lock.read_text() == "pid=1\n"
    assert not old.exists()


def test_prune_unlinks_a_symlink_and_leaves_its_target(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    target = tmp_path / "kept.tar.zst"
    target.write_bytes(PAYLOAD)
    link = config.backup_dir / "last" / "questdb-stale.tar.zst"
    link.symlink_to(os.path.relpath(target, link.parent))
    _set_mtime(link, WHEN - timedelta(days=1000))

    prune(config, WHEN, protect=set())

    assert not os.path.lexists(link)
    assert target.read_bytes() == PAYLOAD


def _finished_archive(config: Config) -> Path:
    path = config.backup_dir / "last" / last_name(config.prefix, WHEN)
    path.write_bytes(PAYLOAD)
    return path


def _retention_config(tmp_path: Path) -> Config:
    return make_config(tmp_path, keep_mins=1440, keep_days=7, keep_weeks=4, keep_months=6)


def _windows(config: Config) -> dict[str, timedelta]:
    return {
        "last": timedelta(minutes=config.keep_mins),
        "daily": timedelta(days=config.keep_days),
        "weekly": timedelta(days=(config.keep_weeks * 7) + 1),
        "monthly": timedelta(days=(config.keep_months * 31) + 1),
    }


def _set_mtime(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)
