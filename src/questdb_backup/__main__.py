"""Command-line entry for the QuestDB backup sidecar."""

import argparse
import logging
import sys
from datetime import datetime

from .backup import ProcessRunner, StopController, TarZstdRunner, run_backup
from .client import SqlClient, make_client
from .config import Config, ConfigError, load_config, next_fire
from .health import HealthServer, HealthState

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="questdb_backup",
        description="Back up a QuestDB open-source server while a checkpoint is held.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one backup and exit.",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config()
        client = make_client(config)
    except ConfigError as exc:
        print(f"questdb-backup: {exc}", file=sys.stderr)
        return 1

    _configure_logging()
    logger.info(
        "questdb-backup client=%s host=%s port=%s schedule=%s tz=%s",
        config.client,
        config.host,
        config.port,
        config.schedule,
        config.tz,
    )
    runner = TarZstdRunner(config.zstd_level, config.zstd_threads)
    health = HealthState()
    stop = StopController()
    server: HealthServer | None = None
    try:
        if config.healthcheck_port != 0:
            try:
                server = HealthServer(config.healthcheck_port, health)
                server.start()
            except OSError as exc:
                logger.error("cannot bind health port %s: %s", config.healthcheck_port, exc)
                return 1
            logger.info("health server on 0.0.0.0:%s", server.port)
        stop.install()
        if args.once:
            return _finish_once(config, client, runner, health, stop)
        return _loop(config, client, runner, health, stop)
    finally:
        stop.restore()
        if server is not None:
            server.close()


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
        force=True,
    )


def _finish_once(
    config: Config,
    client: SqlClient,
    runner: ProcessRunner,
    health: HealthState,
    stop: StopController,
) -> int:
    ok = run_backup(config, client, runner, stop=stop)
    health.mark(ok)
    if stop.signum is not None:
        return 128 + stop.signum
    return 0 if ok else 1


def _loop(
    config: Config,
    client: SqlClient,
    runner: ProcessRunner,
    health: HealthState,
    stop: StopController,
) -> int:
    if config.backup_on_start and not stop.requested:
        _run_marked(config, client, runner, health, stop)
    while not stop.requested:
        upcoming = next_fire(config.schedule, config.tz)
        delay = (upcoming - datetime.now(config.tz)).total_seconds()
        logger.info("next backup at %s", upcoming.isoformat())
        if stop.wait(max(0.0, delay)):
            break
        if stop.requested:
            break
        _run_marked(config, client, runner, health, stop)
    if stop.signum is not None:
        return 128 + stop.signum
    return 0


def _run_marked(
    config: Config,
    client: SqlClient,
    runner: ProcessRunner,
    health: HealthState,
    stop: StopController,
) -> None:
    ok = run_backup(config, client, runner, stop=stop)
    health.mark(ok)
    if not ok and not stop.requested:
        logger.error("backup failed; the process will stay up and try again on the next schedule")


if __name__ == "__main__":
    sys.exit(main())
