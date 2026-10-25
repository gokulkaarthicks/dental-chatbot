"""Command-line interface for the backend.

Commands are also exposed through the ``Makefile``:

    python -m app.cli seed          # make seed
    python -m app.cli reset-demo    # make reset-demo (inside the backend container)
    python -m app.cli init          # create missing tables, seed only if empty
    python -m app.cli demo-status   # print safe fixture counts as JSON

Every command is safe to run against a live database except ``reset-demo``,
which destroys it by design.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.bootstrap import bootstrap_database, create_schema
from app.db import session_scope
from app.observability import configure_logging
from app.services.demo import build_demo_status
from app.services.reset import reset_demo_data
from app.services.seed import seed_database


def _configure_logging() -> None:
    configure_logging()


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_init(_: argparse.Namespace) -> int:
    summary = bootstrap_database(auto_seed=True)
    if summary is None:
        create_schema()
        _print({"status": "schema-ready", "seeded": False})
    else:
        _print({"status": "initialized-and-seeded", "summary": summary.as_dict()})
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    summary = seed_database()
    _print({"status": "seed-complete", "summary": summary.as_dict()})
    return 0


def cmd_reset_demo(args: argparse.Namespace) -> int:
    if args.strategy == "in-place":
        from app.services.reset import reset_demo_in_place

        result = reset_demo_in_place()
    else:
        result = reset_demo_data()
    _print({"status": "reset-complete", **result.as_dict()})
    return 0


def cmd_demo_status(_: argparse.Namespace) -> int:
    with session_scope() as session:
        _print(build_demo_status(session))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Dental receptionist backend utilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create missing tables; seed only if the DB is empty.")
    subparsers.add_parser("seed", help="Insert missing demo fixtures without deleting data.")
    reset_parser = subparsers.add_parser(
        "reset-demo", help="Recreate the demo database and reseed (destructive)."
    )
    reset_parser.add_argument(
        "--strategy",
        choices=("file-swap", "in-place"),
        default="file-swap",
        help="file-swap deletes clinic.db; in-place empties tables inside a transaction.",
    )
    subparsers.add_parser("demo-status", help="Print safe fixture counts as JSON.")
    return parser


COMMANDS = {
    "init": cmd_init,
    "seed": cmd_seed,
    "reset-demo": cmd_reset_demo,
    "demo-status": cmd_demo_status,
}


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        logging.getLogger("app.cli").error("%s failed: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
