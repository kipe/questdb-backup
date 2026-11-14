"""Command-line entry for the QuestDB backup sidecar."""

import argparse
import sys


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
    parser.parse_args(argv)
    print("questdb-backup: the backup runner is not implemented yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
