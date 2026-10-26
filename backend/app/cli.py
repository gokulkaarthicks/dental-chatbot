"""Command-line interface for the backend.

Commands are also exposed through the ``Makefile``:

    python -m app.cli seed                        # make seed
    python -m app.cli reset-demo                  # make reset-demo (in the container)
    python -m app.cli init                         # create missing tables, seed if empty
    python -m app.cli demo-status                 # print safe fixture counts as JSON
    python -m app.cli clinic-list                 # every tenant and its grid
    python -m app.cli create-clinic --slug ...     # add a tenant

Tenancy changes what "run the command" means. ``seed``, ``reset-demo`` and
``demo-status`` all take ``--clinic <slug>``: without it they act on every tenant
they know about, and with it they act on exactly one. That distinction is the
whole point of a per-tenant generation counter -- resetting one demo clinic's
data must not invalidate another clinic's browser state.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app import tenancy
from app.bootstrap import bootstrap_database, create_schema
from app.db import session_scope
from app.models import Clinic
from app.observability import configure_logging
from app.seed_tenants import plan_for_slug
from app.services.demo import build_demo_status
from app.services.reset import reset_demo_data, reset_demo_in_place, reset_tenant_data
from app.services.seed import seed_database


def _configure_logging() -> None:
    configure_logging()


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _slugs(args: argparse.Namespace) -> list[str] | None:
    """``--clinic`` repeated, or ``None`` meaning "every shipped tenant"."""
    return list(args.clinic) if getattr(args, "clinic", None) else None


def cmd_init(args: argparse.Namespace) -> int:
    summary = bootstrap_database(auto_seed=True, slugs=_slugs(args))
    if not summary:
        create_schema()
        _print({"status": "schema-ready", "seeded": False})
    else:
        _print(
            {
                "status": "initialized-and-seeded",
                "summary": {slug: value.as_dict() for slug, value in summary.items()},
            }
        )
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    summary = seed_database(slugs=_slugs(args))
    _print({"status": "seed-complete", "summary": {k: v.as_dict() for k, v in summary.items()}})
    return 0


def cmd_reset_demo(args: argparse.Namespace) -> int:
    slugs = _slugs(args)
    if slugs and len(slugs) == 1:
        # One tenant, in place: only its rows and only its generation counter.
        result = reset_tenant_data(slugs[0])
        _print({"status": "reset-complete", "results": [result.as_dict()]})
        return 0
    if args.strategy == "in-place":
        results = reset_demo_in_place(slugs=slugs)
    else:
        results = reset_demo_data(slugs=slugs)
    _print({"status": "reset-complete", "results": [r.as_dict() for r in results]})
    return 0


def cmd_demo_status(args: argparse.Namespace) -> int:
    slugs = _slugs(args)
    with session_scope() as session:
        contexts = tenancy.contexts_for_slugs(session, slugs)
    # Each tenant is read through its own bound session, so the ORM tenant net and
    # the ``clinic_id`` predicates inside the counts both see one world.
    payloads = []
    for context in contexts:
        with session_scope(context.clinic_id) as session:
            payloads.append(build_demo_status(session, context))
    _print(payloads)
    return 0


def cmd_clinic_list(_: argparse.Namespace) -> int:
    with session_scope() as session:
        rows = list(session.scalars(select(Clinic).order_by(Clinic.id)))
    _print(
        [
            {
                "clinic_id": row.id,
                "slug": row.slug,
                "name": row.name,
                "city": row.city,
                "state": row.state,
                "timezone": row.timezone,
                "slot_minutes": row.slot_minutes,
                "hours": f"{row.open_time:%H:%M}-{row.close_time:%H:%M}",
                "open_weekdays": list(row.open_weekdays or []),
                "seed_horizon_days": row.seed_horizon_days,
                "is_default": row.is_default,
                "active": row.active,
                "has_demo_plan": plan_for_slug(row.slug) is not None,
            }
            for row in rows
        ]
    )
    return 0


def cmd_create_clinic(args: argparse.Namespace) -> int:
    """Add a tenant the shipped demo plans do not cover.

    Deliberately creates the *configuration* only. A brand-new clinic has no
    providers, patients or slots, and inventing them here would mean the CLI
    decided what an unfamiliar practice looks like. ``seed --clinic <slug>``
    fills it in only when a demo plan exists for that slug.
    """
    try:
        ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        logging.getLogger("app.cli").error("Unknown timezone %r", args.timezone)
        return 1
    open_weekdays = tenancy.parse_weekdays(args.open_weekdays)
    if open_weekdays is None:
        logging.getLogger("app.cli").error(
            "Bad --open-weekdays %r: use comma-separated Mon,Tue,... or 0-6",
            args.open_weekdays,
        )
        return 1
    with session_scope() as session:
        clinic = tenancy.create_clinic(
            session,
            slug=args.slug,
            name=args.name,
            city=args.city,
            state=args.state,
            timezone=args.timezone,
            slot_minutes=args.slot_minutes,
            open_time=time.fromisoformat(args.open_time),
            close_time=time.fromisoformat(args.close_time),
            open_weekdays=open_weekdays,
            postal_code=args.postal_code,
            phone=args.phone,
            email=args.email,
            address_line1=args.address_line1,
        )
        session.commit()
        summary = {
            "clinic_id": clinic.id,
            "slug": clinic.slug,
            "name": clinic.name,
            "timezone": clinic.timezone,
            "slot_minutes": clinic.slot_minutes,
            "seeded": False,
        }
    _print({"status": "clinic-created", **summary})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Dental receptionist backend utilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_clinic_flag(target: argparse.ArgumentParser, multiple: bool = True) -> None:
        target.add_argument(
            "--clinic",
            action="append" if multiple else None,
            metavar="SLUG",
            help=(
                "Tenant slug to act on. Repeat for several; omit for every shipped "
                "demo clinic."
            ),
        )

    init_parser = subparsers.add_parser(
        "init", help="Create missing tables; seed only if the DB is empty."
    )
    add_clinic_flag(init_parser)
    seed_parser = subparsers.add_parser(
        "seed", help="Insert missing demo fixtures without deleting data."
    )
    add_clinic_flag(seed_parser)
    reset_parser = subparsers.add_parser(
        "reset-demo", help="Recreate the demo database and reseed (destructive)."
    )
    add_clinic_flag(reset_parser)
    reset_parser.add_argument(
        "--strategy",
        choices=("file-swap", "in-place"),
        default="file-swap",
        help="file-swap deletes clinic.db; in-place empties tables inside a transaction.",
    )
    status_parser = subparsers.add_parser("demo-status", help="Print safe fixture counts as JSON.")
    add_clinic_flag(status_parser)

    subparsers.add_parser("clinic-list", help="List every tenant and its scheduling grid.")

    create = subparsers.add_parser("create-clinic", help="Add a new tenant (configuration only).")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--city", required=True)
    create.add_argument("--state", required=True)
    create.add_argument("--timezone", required=True, help="IANA name, e.g. America/New_York")
    create.add_argument("--slot-minutes", type=int, default=30)
    create.add_argument("--open-time", default="08:00", help="24-hour HH:MM, clinic-local")
    create.add_argument("--close-time", default="18:00", help="24-hour HH:MM, clinic-local")
    create.add_argument(
        "--open-weekdays",
        default="Mon,Tue,Wed,Thu,Fri",
        help="Comma-separated day names or Python weekday ints (0=Mon).",
    )
    create.add_argument("--postal-code", default="")
    create.add_argument("--phone", default="")
    create.add_argument("--email", default="")
    create.add_argument("--address-line1", default="")

    return parser


COMMANDS = {
    "init": cmd_init,
    "seed": cmd_seed,
    "reset-demo": cmd_reset_demo,
    "demo-status": cmd_demo_status,
    "clinic-list": cmd_clinic_list,
    "create-clinic": cmd_create_clinic,
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
