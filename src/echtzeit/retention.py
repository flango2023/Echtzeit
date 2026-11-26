"""Retention for the two logs that hold personal data: rejected payments and API requests.

The GDPR requires that personal data is kept in identifiable form only as long as the purpose
needs it (Regulation (EU) 2016/679, art. 5(1)(e), "storage limitation"). The regulation names no
fixed period, so this module sets none: an operator chooses one per table, and until then
nothing is deleted.

How it is enforced:

* `retention_policy` holds one row per table with the number of days to keep, at least
  `MIN_KEEP_DAYS`. Only `rejected_payments` and `api_requests` are allowed.
* A database trigger refuses to delete any row that is younger than its table's period, and
  refuses every delete when the table has no policy. A bug in this module therefore cannot
  delete recent rows, and updates stay blocked as before.
* Every real purge writes an append-only `retention_runs` row (time, table, period, cutoff, row
  count), so the deletion itself leaves evidence.
* The ledger, transfers and holds are never deleted. They are the money record.

Ages are measured against the database clock in UTC, the same way timestamps are stored.

    python -m echtzeit.retention set api_requests 90 --db sqlite:///echtzeit.db
    python -m echtzeit.retention purge --db sqlite:///echtzeit.db --dry-run
    python -m echtzeit.retention purge --db sqlite:///echtzeit.db

The numbers in these examples are not advice. The right period depends on the purpose and on
the operator's legal duties.

Limit: databases created before this module keep the old trigger that refuses every delete, so
a purge on them fails with "append-only". There is no migration; create a new database.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from echtzeit.models import (
    MIN_KEEP_DAYS,
    RETENTION_COLUMNS,
    RetentionPolicy,
    RetentionRun,
    to_db_time,
)


class RetentionError(Exception):
    """A retention request is invalid."""


@dataclass(frozen=True)
class PurgeResult:
    table_name: str
    keep_days: int | None  # None: no policy, nothing deleted
    cutoff: datetime | None
    rows: int  # rows deleted, or rows that would be deleted in a dry run
    dry_run: bool


def _check_table(table_name: str) -> None:
    if table_name not in RETENTION_COLUMNS:
        allowed = ", ".join(RETENTION_COLUMNS)
        raise RetentionError(f"{table_name} cannot be purged; only {allowed}")


def set_policy(
    sessions: sessionmaker[Session], table_name: str, keep_days: int, *, now: datetime | None = None
) -> None:
    _check_table(table_name)
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < MIN_KEEP_DAYS:
        raise RetentionError(f"keep_days must be a whole number of at least {MIN_KEEP_DAYS}")
    set_at = to_db_time(now or datetime.now(UTC))
    with sessions.begin() as session:
        policy = session.get(RetentionPolicy, table_name)
        if policy is None:
            session.add(RetentionPolicy(table_name=table_name, keep_days=keep_days, set_at=set_at))
        else:
            policy.keep_days, policy.set_at = keep_days, set_at


def policies(sessions: sessionmaker[Session]) -> dict[str, int]:
    with sessions() as session, session.begin():
        rows = session.scalars(select(RetentionPolicy)).all()
        return {row.table_name: row.keep_days for row in rows}


def runs(sessions: sessionmaker[Session]) -> list[RetentionRun]:
    with sessions() as session, session.begin():
        return list(session.scalars(select(RetentionRun).order_by(RetentionRun.id)).all())


def purge(sessions: sessionmaker[Session], *, dry_run: bool = False) -> list[PurgeResult]:
    """Delete log rows older than their table's period, all in one transaction."""
    results = []
    with sessions.begin() as session:
        connection = session.connection()
        ran_at = connection.execute(text("SELECT datetime('now')")).scalar_one()
        for table_name, column in RETENTION_COLUMNS.items():
            policy = session.get(RetentionPolicy, table_name)
            if policy is None:
                results.append(PurgeResult(table_name, None, None, 0, dry_run))
                continue
            cutoff = connection.execute(
                text("SELECT datetime('now', :shift)"), {"shift": f"-{policy.keep_days} days"}
            ).scalar_one()
            # table_name and column come from RETENTION_COLUMNS, a constant, never from input.
            where = f"FROM {table_name} WHERE {column} < :cutoff"
            if dry_run:
                rows = connection.execute(
                    text(f"SELECT count(*) {where}"),
                    {"cutoff": cutoff},
                ).scalar_one()
            else:
                rows = connection.execute(text(f"DELETE {where}"), {"cutoff": cutoff}).rowcount
                session.add(
                    RetentionRun(
                        ran_at=datetime.fromisoformat(ran_at),
                        table_name=table_name,
                        keep_days=policy.keep_days,
                        cutoff=datetime.fromisoformat(cutoff),
                        deleted_rows=rows,
                    )
                )
            results.append(
                PurgeResult(
                    table_name, policy.keep_days, datetime.fromisoformat(cutoff), rows, dry_run
                )
            )
    return results


def _sessions_from_args(args) -> sessionmaker[Session]:
    from echtzeit.db import make_engine, make_session_factory
    from echtzeit.dbkey import load_key_file
    from echtzeit.models import init_db

    key = load_key_file(args.db_key_file) if args.db_key_file else None
    engine = make_engine(args.db, key=key)
    init_db(engine)
    return make_session_factory(engine)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Retention for the personal-data logs.")
    parser.add_argument("--db", default="sqlite:///echtzeit.db", help="database URL")
    parser.add_argument("--db-key-file", type=Path, help="database encryption key file")
    sub = parser.add_subparsers(dest="command", required=True)
    set_cmd = sub.add_parser("set", help="set the retention period of one table")
    set_cmd.add_argument("table", choices=sorted(RETENTION_COLUMNS))
    set_cmd.add_argument("days", type=int)
    sub.add_parser("show", help="show policies and past purges")
    purge_cmd = sub.add_parser("purge", help="delete rows older than their period")
    purge_cmd.add_argument("--dry-run", action="store_true", help="count only, delete nothing")
    args = parser.parse_args(argv)

    sessions = _sessions_from_args(args)
    if args.command == "set":
        try:
            set_policy(sessions, args.table, args.days)
        except RetentionError as exc:
            parser.error(str(exc))
        print(f"{args.table}: keep {args.days} days")
    elif args.command == "show":
        current = policies(sessions)
        for table_name in RETENTION_COLUMNS:
            days = current.get(table_name)
            print(f"{table_name}: " + (f"keep {days} days" if days else "no policy, kept forever"))
        for run in runs(sessions):
            print(
                f"{run.ran_at:%Y-%m-%d %H:%M:%S} UTC  {run.table_name}  keep {run.keep_days} days  "
                f"deleted {run.deleted_rows} rows older than {run.cutoff:%Y-%m-%d %H:%M:%S}"
            )
    else:
        for result in purge(sessions, dry_run=args.dry_run):
            if result.keep_days is None:
                print(f"{result.table_name}: no policy, nothing deleted")
            else:
                verb = "would delete" if result.dry_run else "deleted"
                print(
                    f"{result.table_name}: {verb} {result.rows} rows older than "
                    f"{result.cutoff:%Y-%m-%d %H:%M:%S} UTC (keep {result.keep_days} days)"
                )


if __name__ == "__main__":
    main()
