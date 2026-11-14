"""Environment parsing. Directories are temporary. Nothing is contacted."""

from pathlib import Path

import pytest

from questdb_backup.config import ConfigError, load_config


def test_missing_host_is_rejected() -> None:
    with pytest.raises(ConfigError, match="QUESTDB_HOST"):
        load_config({})


def test_http_client_defaults_port_to_9000(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path, QUESTDB_CLIENT="http"))
    assert config.port == 9000


def test_pg_client_defaults_port_to_8812(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path, QUESTDB_CLIENT="pg"))
    assert config.port == 8812


def test_same_data_and_backup_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "same"
    root.mkdir()
    with pytest.raises(ConfigError, match="must be different"):
        load_config(
            {
                "QUESTDB_HOST": "questdb",
                "QUESTDB_DATA_DIR": str(root),
                "BACKUP_DIR": str(root),
            }
        )


def test_backup_directory_inside_data_directory_is_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(ConfigError, match="must be different"):
        load_config(
            {
                "QUESTDB_HOST": "questdb",
                "QUESTDB_DATA_DIR": str(data),
                "BACKUP_DIR": str(data / "backups"),
            }
        )


def test_backup_on_start_defaults_to_true(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path))
    assert config.backup_on_start is True


def test_backup_on_start_false(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path, BACKUP_ON_START="FALSE"))
    assert config.backup_on_start is False


def test_backup_on_start_rejects_maybe(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="BACKUP_ON_START"):
        load_config(_env(tmp_path, BACKUP_ON_START="maybe"))


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    data = tmp_path / "data"
    backup = tmp_path / "backups"
    data.mkdir()
    backup.mkdir()
    env = {
        "QUESTDB_HOST": "questdb",
        "QUESTDB_DATA_DIR": str(data),
        "BACKUP_DIR": str(backup),
    }
    env.update(extra)
    return env
