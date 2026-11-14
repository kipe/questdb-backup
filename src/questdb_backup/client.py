"""QuestDB SQL clients. HTTP uses the stdlib. Postgres uses psycopg. No shell."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

import psycopg

from .config import Config, ConfigError

SQL_STATUS = "SELECT * FROM checkpoint_status()"

_MAX_ERROR = 300


class SqlError(Exception):
    """QuestDB rejected a statement or the connection failed."""


class SqlClient(Protocol):
    def execute(self, sql: str) -> None:
        """Run one statement. Raise ``SqlError`` on failure."""

    def checkpoint_status(self) -> tuple[bool, datetime | None]:
        """Return ``(in_progress, started_at)`` from ``checkpoint_status()``."""


def make_client(config: Config) -> SqlClient:
    if config.client == "http":
        return HttpSqlClient(config.host, config.port, config.user, config.password)
    return PgSqlClient(config.host, config.port, config.user, config.password, config.database)


def exec_url(host: str, port: int, sql: str) -> str:
    """Build the QuestDB ``/exec`` URL. The SQL is URL-encoded in the query string."""
    scheme, hostname = split_host(host)
    query = urllib.parse.urlencode({"query": sql}, quote_via=urllib.parse.quote)
    return f"{scheme}://{_bracket(hostname)}:{port}/exec?{query}"


def split_host(host: str) -> tuple[str, str]:
    """Return ``(scheme, hostname)``. A bare host uses ``http``."""
    if "://" not in host:
        if not host or "/" in host or _has_port(host):
            raise ConfigError("QUESTDB_HOST must be a hostname or an http(s) URL without a port")
        return "http", host
    parsed = urllib.parse.urlsplit(host)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("QUESTDB_HOST must be a hostname or an http(s) URL")
    if parsed.port is not None:
        raise ConfigError("QUESTDB_HOST must not include a port; set QUESTDB_PORT")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ConfigError("QUESTDB_HOST must not include credentials or a path")
    return parsed.scheme.lower(), parsed.hostname


def parse_checkpoint_status(payload: object) -> tuple[bool, datetime | None]:
    """Read ``in_progress`` and ``started_at`` from a QuestDB JSON result."""
    if not isinstance(payload, Mapping):
        raise SqlError("checkpoint_status response is not an object")
    columns = payload.get("columns")
    dataset = payload.get("dataset")
    if not isinstance(columns, list) or not isinstance(dataset, list):
        raise SqlError("checkpoint_status response is missing columns or dataset")
    if not dataset:
        raise SqlError("checkpoint_status returned no rows")
    names: list[str] = []
    for column in columns:
        if isinstance(column, dict) and isinstance(column.get("name"), str):
            names.append(column["name"])
        elif isinstance(column, str):
            names.append(column)
        else:
            raise SqlError("checkpoint_status column has no name")
    row = dataset[0]
    if not isinstance(row, list):
        raise SqlError("checkpoint_status row is not a list")
    if len(row) < len(names):
        raise SqlError("checkpoint_status row is shorter than the column list")
    values = dict(zip(names, row, strict=False))
    if "in_progress" not in values or "started_at" not in values:
        raise SqlError("checkpoint_status is missing in_progress or started_at")
    return _as_bool(values["in_progress"]), _as_timestamp(values["started_at"])


class HttpSqlClient:
    """GET ``/exec`` with optional basic auth. QuestDB OSS rejects POST."""

    def __init__(self, host: str, port: int, user: str = "", password: str = "", timeout: float = 60) -> None:
        split_host(host)
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._timeout = timeout

    def execute(self, sql: str) -> None:
        self._request(sql)

    def checkpoint_status(self) -> tuple[bool, datetime | None]:
        return parse_checkpoint_status(self._request(SQL_STATUS))

    def _request(self, sql: str) -> object:
        url = exec_url(self._host, self._port, sql)
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", "questdb-backup")
        if self._user or self._password:
            token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = _short(_redact(exc.read().decode("utf-8", errors="replace"), self._password))
            raise SqlError(f"HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise SqlError(f"request failed: {_redact(str(exc.reason), self._password)}") from None
        if status != 200:
            raise SqlError(f"HTTP {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SqlError("response was not JSON") from exc
        if isinstance(payload, dict) and "error" in payload:
            raise SqlError(_short(_redact(str(payload["error"]), self._password)))
        return payload


class PgSqlClient:
    """One short psycopg connection per statement. An empty password is passed through."""

    def __init__(self, host: str, port: int, user: str, password: str, database: str) -> None:
        if "://" in host or "/" in host:
            raise ConfigError("QUESTDB_HOST for the pg client must be a hostname")
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database

    def execute(self, sql: str) -> None:
        self._query(sql)

    def checkpoint_status(self) -> tuple[bool, datetime | None]:
        names, rows = self._query(SQL_STATUS)
        if not rows:
            raise SqlError("checkpoint_status returned no rows")
        values = dict(zip(names, rows[0], strict=False))
        if "in_progress" not in values or "started_at" not in values:
            raise SqlError("checkpoint_status is missing in_progress or started_at")
        return _as_bool(values["in_progress"]), _as_timestamp(values["started_at"])

    def _query(self, sql: str) -> tuple[list[str], list[tuple[object, ...]]]:
        try:
            with (
                psycopg.connect(
                    host=self._host,
                    port=self._port,
                    user=self._user,
                    password=self._password,
                    dbname=self._database,
                    autocommit=True,
                    connect_timeout=30,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(sql)
                if cursor.description is None:
                    return [], []
                names = [column.name for column in cursor.description]
                return names, list(cursor.fetchall())
        except psycopg.Error as exc:
            raise SqlError(_short(_redact(str(exc), self._password))) from None


def _has_port(host: str) -> bool:
    """True when a bare host looks like ``name:port`` rather than an IPv6 address."""
    if host.startswith("["):
        return False
    if host.count(":") != 1:
        return False
    return host.rsplit(":", 1)[1].isdecimal()


def _bracket(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false", "t", "f"}:
        return value.lower() in {"true", "t"}
    raise SqlError(f"unexpected in_progress value: {value!r}")


def _as_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise SqlError(f"cannot parse started_at {value!r}") from exc
    # QuestDB's native timestamp is microseconds since the Unix epoch.
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    if isinstance(value, float):
        return datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    raise SqlError(f"cannot parse started_at {value!r}")


def _redact(text: str, secret: str) -> str:
    if secret:
        return text.replace(secret, "***")
    return text


def _short(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) > _MAX_ERROR:
        return f"{flat[:_MAX_ERROR]}..."
    return flat
