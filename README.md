# questdb-backup

Sidecar for QuestDB open source. On each run it will:

1. Run `CHECKPOINT CREATE`.
2. Write a zstd-compressed tar of the whole QuestDB root while the checkpoint is held.
3. Run `CHECKPOINT RELEASE` even if the copy fails.
4. Rotate archives.

The folder layout and retention knobs follow [prodrigestivill/postgres-backup-local](https://github.com/prodrigestivill/docker-postgres-backup-local) (MIT).

QuestDB documents the statement and the open-source procedure:

- [CHECKPOINT](https://questdb.com/docs/query/sql/checkpoint/)
- [Backup](https://questdb.com/docs/operations/backup/)

The image is [`kipe/questdb-backup`](https://hub.docker.com/r/kipe/questdb-backup) for `linux/amd64` and `linux/arm64`. [`0.1.0`](https://github.com/kipe/questdb-backup/releases/tag/v0.1.0) is the current release. `:latest` points at that same image.

## Docker Compose

The backup process runs as root. QuestDB files are often not world-readable, and the data mount is read-only, so the process still needs permission to read those files.

```yaml
services:
  questdb:
    image: questdb/questdb:latest
    volumes:
      - questdb-data:/var/lib/questdb
    restart: unless-stopped

  questdb-backup:
    image: kipe/questdb-backup:0.1.0
    restart: unless-stopped
    environment:
      QUESTDB_HOST: questdb
      QUESTDB_USER: admin
      QUESTDB_PASSWORD: change-me
      SCHEDULE: "0 3 * * *"
      BACKUP_ON_START: "TRUE"
      BACKUP_KEEP_DAYS: "7"
      BACKUP_KEEP_WEEKS: "4"
      BACKUP_KEEP_MONTHS: "6"
      HEALTHCHECK_PORT: "8080"
    volumes:
      - questdb-data:/var/lib/questdb:ro
      - questdb-backups:/backups

volumes:
  questdb-data:
  questdb-backups:
```

Any variable omitted above uses the default in the table.

[`docker-compose.example.yml`](docker-compose.example.yml) is a local end-to-end stack. It builds this Dockerfile, starts QuestDB 10.2.0 with HTTP auth, and takes a backup once QuestDB is healthy.

```bash
docker compose -f docker-compose.example.yml up --build
docker compose -f docker-compose.example.yml exec questdb-backup ls -l /backups/last
```

The console is on port 9000 and the backup health port is 8080. The username and password in that file are `questdb`.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `QUESTDB_HOST` | none | QuestDB hostname. |
| `QUESTDB_CLIENT` | `http` | `http` or `pg`. |
| `QUESTDB_PORT` | `9000` for http, `8812` for pg | Port for the chosen client. |
| `QUESTDB_USER` | empty | Login user. May be empty. |
| `QUESTDB_PASSWORD` | empty | Login password. May be empty. |
| `QUESTDB_DATABASE` | `qdb` | Database name for the pg client. |
| `QUESTDB_DATA_DIR` | `/var/lib/questdb` | QuestDB server root. Mount it read-only. |
| `BACKUP_DIR` | `/backups` | Archive directory. Must be a different path from the data directory. |
| `BACKUP_PREFIX` | `questdb` | Filename prefix. |
| `SCHEDULE` | `0 3 * * *` | Cron schedule, evaluated in `TZ`. |
| `TZ` | `UTC` | Timezone for `SCHEDULE`. |
| `BACKUP_ON_START` | `TRUE` | Take a backup when the process starts. postgres-backup-local defaults this to `FALSE`. |
| `BACKUP_KEEP_DAYS` | `7` | How long to keep `daily/` archives, in days. |
| `BACKUP_KEEP_WEEKS` | `4` | Weekly retention. See Retention. |
| `BACKUP_KEEP_MONTHS` | `6` | Monthly retention. See Retention. |
| `BACKUP_KEEP_MINS` | `1440` | How long to keep `last/` archives, in minutes. |
| `BACKUP_LATEST_TYPE` | `symlink` | How to publish `<prefix>-latest.tar.zst`. Also `hardlink` or `none`. |
| `HEALTHCHECK_PORT` | `8080` | Port for health checks. |
| `ZSTD_LEVEL` | `3` | zstd compression level. |
| `ZSTD_THREADS` | `1` | zstd worker threads. |

## Folder layout

`<prefix>` is `BACKUP_PREFIX`.

- `last/<prefix>-YYYYMMDD-HHMMSS.tar.zst` and `last/<prefix>-latest.tar.zst`
- `daily/<prefix>-YYYYMMDD.tar.zst`
- `weekly/<prefix>-YYYYww.tar.zst` (ISO week)
- `monthly/<prefix>-YYYYMM.tar.zst`

The timestamped file in `last/` is hardlinked into `daily/`, `weekly/`, and `monthly/`. If the hardlink fails, the file is copied. Copying stores about four full archives. Put `/backups` on one real filesystem (not an Unraid user share) if you want hardlinks.

## Retention

Archives are pruned only after a successful backup.

- Minutes apply to `last/` (`BACKUP_KEEP_MINS`).
- Days apply to `daily/` (`BACKUP_KEEP_DAYS`).
- Weeks are counted as `(BACKUP_KEEP_WEEKS * 7) + 1` days.
- Months are counted as `(BACKUP_KEEP_MONTHS * 31) + 1` days.

Files older than that cutoff are removed.

## Checkpoint safety

The lockfile is `/backups/.checkpoint-lock`.

If a checkpoint is already held and the lockfile is absent, the run aborts and does not release it.

If the lockfile is ours, the next start releases the checkpoint, deletes the partial archive, and continues.

`CHECKPOINT RELEASE` runs even when tar fails. A file in `last/` is renamed into place only after tar and release both succeed. If release fails, the lockfile stays so the next start can recover.

## Restore

There is no restore command. To restore an archive:

1. Stop QuestDB.
2. Extract the archive onto an empty server root.
3. `touch _restore` in that root.
4. Start the same major version of QuestDB.

The open-source steps are in [Restore to a saved checkpoint](https://questdb.com/docs/operations/backup/#restore-to-a-saved-checkpoint). That section uses `_restore`. Enterprise restore uses a different trigger file.

## One shot

```bash
python -m questdb_backup --once
```

`--once` runs a single backup and exits. Without `--once`, the process waits on `SCHEDULE` and, when `BACKUP_ON_START` is `TRUE`, also backs up once at startup.

## Limitations

- Each run copies the whole QuestDB root.
- The checkpoint stays open for the whole compress.
- Does not run on Windows.
- QuestDB allows only one checkpoint at a time.
- This is not QuestDB Enterprise `BACKUP DATABASE`.

## Releases

A GitHub tag `v*` publishes `kipe/questdb-backup:<version>` and moves `:latest`. `v0.1.0` is [`0.1.0`](https://hub.docker.com/r/kipe/questdb-backup/tags). The workflow logs in to Docker Hub with repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.
