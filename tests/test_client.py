"""QuestDB payload parsing. No sockets."""

from datetime import UTC, datetime

import pytest

from questdb_backup.client import exec_url, parse_checkpoint_status, split_host
from questdb_backup.config import ConfigError


def test_parse_checkpoint_status_bool_and_null() -> None:
    payload = {
        "columns": [
            {"name": "in_progress", "type": "BOOLEAN"},
            {"name": "started_at", "type": "TIMESTAMP"},
        ],
        "dataset": [[False, None]],
        "count": 1,
    }
    assert parse_checkpoint_status(payload) == (False, None)


def test_parse_checkpoint_status_microsecond_timestamp() -> None:
    started = datetime(2026, 3, 20, 14, 30, tzinfo=UTC)
    payload = {
        "columns": [
            {"name": "in_progress", "type": "BOOLEAN"},
            {"name": "started_at", "type": "TIMESTAMP"},
        ],
        "dataset": [[True, 1774017000000000]],
    }
    assert parse_checkpoint_status(payload) == (True, started)


def test_exec_url_encodes_sql_and_brackets_ipv6() -> None:
    url = exec_url("2001:db8::1", 9000, "SELECT * FROM checkpoint_status()")
    assert url == "http://[2001:db8::1]:9000/exec?query=SELECT%20%2A%20FROM%20checkpoint_status%28%29"


def test_split_host_rejects_a_port() -> None:
    with pytest.raises(ConfigError, match="port"):
        split_host("host:9000")


def test_split_host_accepts_https_url() -> None:
    assert split_host("https://questdb.example") == ("https", "questdb.example")
