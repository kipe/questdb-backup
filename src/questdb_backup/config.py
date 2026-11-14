"""Environment configuration for the backup sidecar."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

LOCK_NAME = ".checkpoint-lock"
BACKUP_BUCKETS = ("last", "daily", "weekly", "monthly")


class ConfigError(Exception):
    """The process cannot start with this configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    host: str
    client: str
    port: int
    user: str
    password: str
    database: str
    data_dir: Path
    backup_dir: Path
    prefix: str
    schedule: str
    tz: ZoneInfo
    backup_on_start: bool
    keep_days: int
    keep_weeks: int
    keep_months: int
    keep_mins: int
    latest_type: str
    healthcheck_port: int
    zstd_level: int
    zstd_threads: int


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Read environment variables, check the directories, and create bucket folders."""
    env = os.environ if environ is None else environ
    host = _required(env, "QUESTDB_HOST")
    client = _choice(env, "QUESTDB_CLIENT", "http", {"http", "pg"})
    port_default = 9000 if client == "http" else 8812
    port = _bounded(env, "QUESTDB_PORT", port_default, minimum=1, maximum=65535)
    user = env.get("QUESTDB_USER", "")
    password = env.get("QUESTDB_PASSWORD", "")
    database = _setting(env, "QUESTDB_DATABASE", "qdb")
    prefix = _prefix(_setting(env, "BACKUP_PREFIX", "questdb"))
    schedule = _schedule(_setting(env, "SCHEDULE", "0 3 * * *"))
    tz = _timezone(_setting(env, "TZ", "UTC"))
    backup_on_start = _true_false(env, "BACKUP_ON_START", "TRUE")
    keep_days = _bounded(env, "BACKUP_KEEP_DAYS", 7, minimum=0)
    keep_weeks = _bounded(env, "BACKUP_KEEP_WEEKS", 4, minimum=0)
    keep_months = _bounded(env, "BACKUP_KEEP_MONTHS", 6, minimum=0)
    keep_mins = _bounded(env, "BACKUP_KEEP_MINS", 1440, minimum=0)
    latest_type = _choice(env, "BACKUP_LATEST_TYPE", "symlink", {"symlink", "hardlink", "none"})
    healthcheck_port = _bounded(env, "HEALTHCHECK_PORT", 8080, minimum=0, maximum=65535)
    zstd_level = _bounded(env, "ZSTD_LEVEL", 3, minimum=1, maximum=19)
    zstd_threads = _bounded(env, "ZSTD_THREADS", 1, minimum=1)
    data_dir, backup_dir = _prepare_directories(
        _setting(env, "QUESTDB_DATA_DIR", "/var/lib/questdb"),
        _setting(env, "BACKUP_DIR", "/backups"),
    )
    return Config(
        host=host,
        client=client,
        port=port,
        user=user,
        password=password,
        database=database,
        data_dir=data_dir,
        backup_dir=backup_dir,
        prefix=prefix,
        schedule=schedule,
        tz=tz,
        backup_on_start=backup_on_start,
        keep_days=keep_days,
        keep_weeks=keep_weeks,
        keep_months=keep_months,
        keep_mins=keep_mins,
        latest_type=latest_type,
        healthcheck_port=healthcheck_port,
        zstd_level=zstd_level,
        zstd_threads=zstd_threads,
    )


def next_fire(schedule: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Return the next schedule time after ``now``, in ``tz``."""
    current = now if now is not None else datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    upcoming = croniter(schedule, current).get_next(datetime)
    if upcoming.tzinfo is None:
        return upcoming.replace(tzinfo=tz)
    return upcoming.astimezone(tz)


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _setting(env: Mapping[str, str], key: str, default: str) -> str:
    if key not in env:
        return default
    value = env[key].strip()
    if not value:
        raise ConfigError(f"{key} is empty")
    return value


def _choice(env: Mapping[str, str], key: str, default: str, allowed: set[str]) -> str:
    value = _setting(env, key, default).lower()
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ConfigError(f"{key} must be one of: {options}")
    return value


def _bounded(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if key not in env:
        value = default
    else:
        text = env[key].strip()
        if not text.isdecimal():
            raise ConfigError(f"{key} must be an integer >= {minimum}")
        value = int(text)
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ConfigError(f"{key} must be an integer >= {minimum}")
        raise ConfigError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _true_false(env: Mapping[str, str], key: str, default: str) -> bool:
    value = _setting(env, key, default).upper()
    if value == "TRUE":
        return True
    if value == "FALSE":
        return False
    raise ConfigError(f"{key} must be TRUE or FALSE")


def _prefix(value: str) -> str:
    if not value or "/" in value or ".." in value:
        raise ConfigError("BACKUP_PREFIX must not be empty and must not contain '/' or '..'")
    return value


def _schedule(value: str) -> str:
    if value.lower() == "@daily":
        if not croniter.is_valid("@daily"):
            raise ConfigError("SCHEDULE @daily is not supported")
        return "@daily"
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise ConfigError("SCHEDULE must be a 5-field cron expression or @daily")
    return value


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"TZ is not a known timezone: {value}") from exc


def _prepare_directories(data_text: str, backup_text: str) -> tuple[Path, Path]:
    data_dir = Path(data_text)
    backup_dir = Path(backup_text)
    if not data_dir.is_dir() or not os.access(data_dir, os.R_OK | os.X_OK):
        raise ConfigError(f"QUESTDB_DATA_DIR is not a readable directory: {data_dir}")
    try:
        next(data_dir.iterdir(), None)
    except OSError as exc:
        raise ConfigError(f"QUESTDB_DATA_DIR is not a readable directory: {data_dir}") from exc
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create BACKUP_DIR {backup_dir}") from exc
    if not backup_dir.is_dir() or not os.access(backup_dir, os.W_OK | os.X_OK):
        raise ConfigError(f"BACKUP_DIR is not a writable directory: {backup_dir}")
    try:
        resolved_data = data_dir.resolve()
        resolved_backup = backup_dir.resolve()
        same = os.path.samefile(resolved_data, resolved_backup)
    except OSError as exc:
        raise ConfigError("cannot compare QUESTDB_DATA_DIR and BACKUP_DIR") from exc
    nested = resolved_data.is_relative_to(resolved_backup) or resolved_backup.is_relative_to(resolved_data)
    if resolved_data == resolved_backup or same or nested:
        raise ConfigError("QUESTDB_DATA_DIR and BACKUP_DIR must be different paths, and neither may contain the other")
    for bucket in BACKUP_BUCKETS:
        target = resolved_backup / bucket
        try:
            target.mkdir(exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"cannot create {target}") from exc
    return resolved_data, resolved_backup
