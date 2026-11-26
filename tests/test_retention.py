"""Retention of the personal-data logs: purge only what is old enough, and fail closed."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from helpers import BANK_B, message
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from echtzeit.models import MIN_KEEP_DAYS
from echtzeit.retention import RetentionError, main, policies, purge, runs, set_policy

NOW = datetime.now(UTC)


def _request(audit, days_ago: float, route: str = "/v1/health") -> None:
    audit.record_request(
        at=NOW - timedelta(days=days_ago),
        request_id=f"r-{days_ago}-{route}",
        client_id="bank-a",
        key_id="k1",
        method="GET",
        route=route,
        status=200,
        duration_ms=1,
        source=None,
    )


def _rejected(processor, customer, clock, days_ago: float) -> None:
    clock.now = NOW - timedelta(days=days_ago)
    alice = customer("Alice", funds=0)
    bob = customer("Bob", bic=BANK_B)
    tag = f"OLD-{days_ago}"
    result = processor.process_transfer(
        message(alice, bob, msg_id=tag, tx_id=tag, accepted_at=clock.now, created_at=clock.now)
    )
    assert result.report.reason_code == "MS03"  # insufficient funds, so it is logged


def _count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # nosec


def test_without_a_policy_nothing_is_deleted_even_by_hand(engine, sessions, audit):
    _request(audit, days_ago=4000)
    results = purge(sessions)
    assert [(r.table_name, r.keep_days, r.rows) for r in results] == [
        ("rejected_payments", None, 0),
        ("api_requests", None, 0),
    ]
    with pytest.raises(DatabaseError, match="retention period"), engine.begin() as connection:
        connection.execute(text("DELETE FROM api_requests"))
    assert _count(engine, "api_requests") == 1
    assert runs(sessions) == []


def test_a_purge_deletes_only_rows_older_than_the_period(engine, sessions, audit):
    for days in (200, 120, 91, 89, 30, 0):
        _request(audit, days_ago=days)
    set_policy(sessions, "api_requests", 90)

    dry = purge(sessions, dry_run=True)
    assert [(r.table_name, r.rows) for r in dry if r.keep_days] == [("api_requests", 3)]
    assert _count(engine, "api_requests") == 6 and runs(sessions) == []

    done = {r.table_name: r for r in purge(sessions)}
    assert done["api_requests"].rows == 3 and not done["api_requests"].dry_run
    assert done["rejected_payments"].keep_days is None
    kept = sorted(
        (NOW.replace(tzinfo=None) - datetime.fromisoformat(r["at"])).days for r in audit.requests()
    )
    assert kept == [0, 30, 89]

    (run,) = runs(sessions)
    assert (run.table_name, run.keep_days, run.deleted_rows) == ("api_requests", 90, 3)
    assert abs((run.ran_at - run.cutoff) - timedelta(days=90)) < timedelta(seconds=1)
    assert purge(sessions)[1].rows == 0  # a second run finds nothing more
    assert len(runs(sessions)) == 2


def test_the_trigger_refuses_recent_rows_even_with_a_policy(engine, sessions, audit):
    _request(audit, days_ago=10)
    _request(audit, days_ago=100)
    set_policy(sessions, "api_requests", 90)
    with pytest.raises(DatabaseError, match="retention period"), engine.begin() as connection:
        connection.execute(text("DELETE FROM api_requests"))
    assert _count(engine, "api_requests") == 2  # the refused statement deleted nothing
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM api_requests WHERE at < datetime('now', '-95 days')"))
    assert _count(engine, "api_requests") == 1


def test_rejected_payments_follow_their_own_policy(engine, sessions, processor, customer, clock):
    _rejected(processor, customer, clock, days_ago=400)
    _rejected(processor, customer, clock, days_ago=5)
    set_policy(sessions, "api_requests", 30)
    assert purge(sessions)[0].rows == 0  # no policy for rejected payments yet
    set_policy(sessions, "rejected_payments", 365)
    assert purge(sessions)[0].rows == 1
    assert _count(engine, "rejected_payments") == 1


def test_logs_stay_unchangeable_and_the_money_record_is_never_purged(engine, sessions, audit):
    _request(audit, days_ago=500)
    set_policy(sessions, "api_requests", 30)
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as connection:
        connection.execute(text("UPDATE api_requests SET status = 500"))
    purge(sessions)
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as connection:
        connection.execute(text("DELETE FROM retention_runs"))
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as connection:
        connection.execute(text("UPDATE retention_runs SET deleted_rows = 0"))
    for table in ("ledger_entries", "transfers", "holds"):
        with pytest.raises(RetentionError, match="cannot be purged"):
            set_policy(sessions, table, 365)


def test_policies_below_the_minimum_or_for_other_tables_are_refused(engine, sessions):
    for days in (0, -1, MIN_KEEP_DAYS - 1, 90.5, True):
        with pytest.raises(RetentionError, match="at least"):
            set_policy(sessions, "api_requests", days)
    assert policies(sessions) == {}
    # The database checks the same rules if this module is bypassed.
    for statement in (
        "INSERT INTO retention_policy VALUES ('api_requests', 1, '2026-01-01')",
        "INSERT INTO retention_policy VALUES ('ledger_entries', 365, '2026-01-01')",
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement))
    set_policy(sessions, "api_requests", 90)
    set_policy(sessions, "api_requests", 60)
    assert policies(sessions) == {"api_requests": 60}


def test_the_cli_sets_shows_and_purges(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    main(["--db", url, "set", "api_requests", "90"])
    main(["--db", url, "purge", "--dry-run"])
    main(["--db", url, "purge"])
    main(["--db", url, "show"])
    out = capsys.readouterr().out
    assert "api_requests: keep 90 days" in out
    assert "api_requests: would delete 0 rows" in out
    assert "rejected_payments: no policy, nothing deleted" in out
    assert "rejected_payments: no policy, kept forever" in out
    assert "deleted 0 rows older than" in out
    with pytest.raises(SystemExit):
        main(["--db", url, "set", "api_requests", "7"])
    with pytest.raises(SystemExit):
        main(["--db", url, "set", "ledger_entries", "90"])


def test_rejections_are_stored_in_utc_so_their_age_is_measured_correctly(
    processor, customer, clock, audit
):
    berlin = timezone(timedelta(hours=1))
    clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=berlin)
    alice, bob = customer("Alice"), customer("Bob", bic=BANK_B)
    processor.process_transfer(
        message(alice, bob, accepted_at=clock.now, created_at=clock.now, tx_id="TZ-1")
    )
    (record,) = audit.rejections()
    assert record.recorded_at.replace(tzinfo=None) == datetime(2026, 11, 24, 11, 0)
