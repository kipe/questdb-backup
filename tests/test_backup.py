"""Checkpoint state machine. The SQL client and the archive writer are fakes."""

from pathlib import Path

from pytest import MonkeyPatch

from questdb_backup.backup import ArchiveError, run_backup
from questdb_backup.client import SqlError
from tests.support import WHEN, FakeClient, FakeRunner, make_config

ARCHIVE = "questdb-20260320-143000.tar.zst"


def test_successful_backup_publishes_archive_and_clears_lock(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeClient()
    runner = FakeRunner()
    last = config.backup_dir / "last"

    assert run_backup(config, client, runner, now=lambda: WHEN) is True

    archive = last / ARCHIVE
    assert client.calls == ["CHECKPOINT CREATE", "CHECKPOINT RELEASE"]
    assert runner.destinations == [last / f"{ARCHIVE}.partial"]
    assert archive.read_bytes() == b"archive-bytes"
    assert list(last.glob("*.partial")) == []
    assert not (config.backup_dir / ".checkpoint-lock").exists()


def test_foreign_checkpoint_is_left_alone(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    last = config.backup_dir / "last"
    archive = last / "questdb-20200101-000000.tar.zst"
    partial = last / "questdb-20200101-000000.tar.zst.partial"
    archive.write_bytes(b"keep")
    partial.write_bytes(b"partial")
    client = FakeClient(in_progress=True, started_at=WHEN)
    runner = FakeRunner()

    assert run_backup(config, client, runner, now=lambda: WHEN) is False

    assert client.calls == []
    assert runner.destinations == []
    assert archive.read_bytes() == b"keep"
    assert partial.read_bytes() == b"partial"
    assert not (config.backup_dir / ".checkpoint-lock").exists()


def test_own_lock_during_checkpoint_releases_then_backs_up(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    last = config.backup_dir / "last"
    partial = last / "questdb-20200101-000000.tar.zst.partial"
    partial.write_bytes(b"partial")
    notes = last / "notes.txt"
    notes.write_text("keep")
    lock = config.backup_dir / ".checkpoint-lock"
    lock.write_text("ours\n")
    client = FakeClient(in_progress=True)
    first: list[tuple[str, bool]] = []

    def on_execute(sql: str) -> None:
        if not client.calls:
            first.append((sql, partial.exists()))

    client.on_execute = on_execute

    assert run_backup(config, client, FakeRunner(), now=lambda: WHEN) is True

    assert first == [("CHECKPOINT RELEASE", True)]
    assert client.calls == ["CHECKPOINT RELEASE", "CHECKPOINT CREATE", "CHECKPOINT RELEASE"]
    assert not partial.exists()
    assert notes.read_text() == "keep"
    assert (last / ARCHIVE).read_bytes() == b"archive-bytes"
    assert not lock.exists()


def test_stale_lock_is_removed_without_release(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    last = config.backup_dir / "last"
    partial = last / "questdb-20200101-000000.tar.zst.partial"
    partial.write_bytes(b"partial")
    lock = config.backup_dir / ".checkpoint-lock"
    lock.write_text("stale\n")
    client = FakeClient(in_progress=False)
    first: list[tuple[str, bool, str]] = []

    def on_execute(sql: str) -> None:
        if not client.calls:
            text = lock.read_text() if lock.exists() else ""
            first.append((sql, partial.exists(), text))

    client.on_execute = on_execute

    assert run_backup(config, client, FakeRunner(), now=lambda: WHEN) is True

    sql, partial_existed, lock_text = first[0]
    assert sql == "CHECKPOINT CREATE"
    assert partial_existed is False
    assert "stale" not in lock_text
    assert client.calls == ["CHECKPOINT CREATE", "CHECKPOINT RELEASE"]
    assert (last / ARCHIVE).is_file()
    assert not lock.exists()


def test_archive_error_releases_checkpoint_and_drops_partial(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeClient()
    runner = FakeRunner(error=ArchiveError("tar failed"))
    last = config.backup_dir / "last"

    assert run_backup(config, client, runner, now=lambda: WHEN) is False

    assert client.calls == ["CHECKPOINT CREATE", "CHECKPOINT RELEASE"]
    assert runner.destinations == [last / f"{ARCHIVE}.partial"]
    assert not runner.destinations[0].exists()
    assert not (last / ARCHIVE).exists()
    assert not (config.backup_dir / ".checkpoint-lock").exists()


def test_release_failure_keeps_the_lock(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeClient(error=SqlError("release failed"), error_on="CHECKPOINT RELEASE")
    runner = FakeRunner()
    last = config.backup_dir / "last"

    assert run_backup(config, client, runner, now=lambda: WHEN) is False

    assert client.calls == ["CHECKPOINT CREATE", "CHECKPOINT RELEASE"]
    assert not (last / f"{ARCHIVE}.partial").exists()
    assert not (last / ARCHIVE).exists()
    assert (config.backup_dir / ".checkpoint-lock").is_file()


def test_rotation_failure_keeps_published_archive(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("rotation failed")

    monkeypatch.setattr("questdb_backup.backup.rotate", fail)
    config = make_config(tmp_path)
    client = FakeClient()
    last = config.backup_dir / "last"

    assert run_backup(config, client, FakeRunner(), now=lambda: WHEN) is False

    assert (last / ARCHIVE).read_bytes() == b"archive-bytes"
    assert list(last.glob("*.partial")) == []
    assert not (config.backup_dir / ".checkpoint-lock").exists()
    assert client.calls == ["CHECKPOINT CREATE", "CHECKPOINT RELEASE"]
