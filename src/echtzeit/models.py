"""Database tables.

Money is stored as integer euro cents. Ledger entries are signed: a debit is
negative, a credit is positive, and the entries of one transfer sum to zero.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from echtzeit.db import Base

CUSTOMER = "CUSTOMER"
SYSTEM = "SYSTEM"


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


def to_db_time(moment: datetime) -> datetime:
    """Timestamps are stored as naive UTC. An aware value is converted; a naive one is taken as UTC."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("kind IN ('CUSTOMER', 'SYSTEM')", name="ck_account_kind"),
        # Customer accounts can never go negative. System accounts (the source of
        # fictional money) can, because they hold the opposite side of every deposit.
        CheckConstraint("kind = 'SYSTEM' OR balance_cents >= 0", name="ck_account_no_overdraft"),
        # Held money is a part of the balance. System accounts cannot be held.
        CheckConstraint("held_cents >= 0", name="ck_account_held_nonnegative"),
        CheckConstraint(
            "kind = 'SYSTEM' OR held_cents <= balance_cents", name="ck_account_held_within_balance"
        ),
        CheckConstraint("kind <> 'SYSTEM' OR held_cents = 0", name="ck_account_system_not_held"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    holder_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default=CUSTOMER)
    # Payment identifiers. Only customer accounts have them; the system account has neither.
    iban: Mapped[str | None] = mapped_column(String(34), unique=True, nullable=True)
    bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    # Cached balance. The ledger is the source of truth; verify_invariants checks both agree.
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    # Cached sum of active holds. Spendable money is balance_cents - held_cents.
    held_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_transfer_positive"),
        CheckConstraint("debtor_id <> creditor_id", name="ck_transfer_distinct"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    debtor_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    creditor_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    reference: Mapped[str | None] = mapped_column(String(140), nullable=True)
    # Identifiers from the payment message, kept so a payment can be traced end to end.
    end_to_end_id: Mapped[str | None] = mapped_column(String(35), nullable=True)
    # A UETR identifies one payment, so it is unique. Transfers made without a message have none.
    uetr: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (CheckConstraint("amount_cents <> 0", name="ck_entry_nonzero"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Hold(Base):
    """Money stopped on an account. Immutable. It is active until a HoldRelease row exists."""

    __tablename__ = "holds"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_hold_positive"),
        CheckConstraint("amount_cents <= requested_cents", name="ck_hold_within_request"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    requested_cents: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    placed_by: Mapped[str] = mapped_column(String(100))
    # UETR of the payment that triggered the hold, so a trace can tell its own holds apart.
    uetr: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Highest ledger entry id when the hold was placed. Entries up to this id came before it.
    placed_after_entry_id: Mapped[int] = mapped_column(Integer)
    placed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class HoldRelease(Base):
    """Ends a hold. At most one per hold, enforced by the primary key."""

    __tablename__ = "hold_releases"

    hold_id: Mapped[str] = mapped_column(ForeignKey("holds.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(200))
    released_by: Mapped[str] = mapped_column(String(100))
    released_after_entry_id: Mapped[int] = mapped_column(Integer)
    released_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RejectedPayment(Base):
    """Audit record of a payment message that was refused. One row per attempt.

    Rejected payments move no money, so they never reach the ledger. This table keeps them,
    because repeated refusals (probing unknown accounts, repeated insufficient-funds attempts)
    are signals for fraud detection and evidence for audits. The IBANs are the ones written in
    the message, even when no account matches them.
    """

    __tablename__ = "rejected_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    msg_id: Mapped[str] = mapped_column(String(35))
    tx_id: Mapped[str] = mapped_column(String(35), index=True)
    uetr: Mapped[str] = mapped_column(String(36), index=True)
    end_to_end_id: Mapped[str] = mapped_column(String(35))
    debtor_bic: Mapped[str] = mapped_column(String(11))
    debtor_iban: Mapped[str] = mapped_column(String(34), index=True)
    creditor_bic: Mapped[str] = mapped_column(String(11))
    creditor_iban: Mapped[str] = mapped_column(String(34), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(4), index=True)
    reply_msg_id: Mapped[str] = mapped_column(String(35))


class ApiRequest(Base):
    """One API call: who made it, which endpoint, the outcome. Append-only.

    No request bodies, keys or path values are stored. The route is the template
    (for example /v1/accounts/{iban}), so IBANs do not end up in this log. The source address is
    kept only for calls that failed authentication, to investigate guessing attempts.
    """

    __tablename__ = "api_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    client_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    method: Mapped[str] = mapped_column(String(10))
    route: Mapped[str] = mapped_column(String(200))
    status: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)


# Logs that hold personal data and may be purged after a retention period, with the column
# that dates each row. Nothing else is ever deleted: the ledger, transfers and holds are the
# money record.
RETENTION_COLUMNS = {"rejected_payments": "recorded_at", "api_requests": "at"}

# Shortest retention a policy may set, in days. A project choice: a typing mistake must not be
# able to wipe last week's evidence.
MIN_KEEP_DAYS = 30


class RetentionPolicy(Base):
    """How many days rows of one log table are kept. No row means: keep forever (fail closed)."""

    __tablename__ = "retention_policy"
    __table_args__ = (
        CheckConstraint(
            "table_name IN (" + ", ".join(f"'{t}'" for t in RETENTION_COLUMNS) + ")",
            name="retention_table_known",
        ),
        CheckConstraint(f"keep_days >= {MIN_KEEP_DAYS}", name="retention_minimum"),
    )

    table_name: Mapped[str] = mapped_column(String(40), primary_key=True)
    keep_days: Mapped[int] = mapped_column(Integer)
    set_at: Mapped[datetime] = mapped_column(DateTime)


class RetentionRun(Base):
    """One purge of one table: when, under which policy, how many rows went. Append-only."""

    __tablename__ = "retention_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime)
    table_name: Mapped[str] = mapped_column(String(40))
    keep_days: Mapped[int] = mapped_column(Integer)
    cutoff: Mapped[datetime] = mapped_column(DateTime)
    deleted_rows: Mapped[int] = mapped_column(Integer)


def _block(table: str, op: str, when: str = "", message: str | None = None) -> DDL:
    message = message or f"{table} is append-only"
    return DDL(
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} "
        f"BEFORE {op} ON {table} {when}"
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
    ).execute_if(dialect="sqlite")


# Append-only enforcement. Corrections must be new transfers, never edits. The table and column
# names below are constants of this module, never user input.
for _table in (
    "ledger_entries",
    "transfers",
    "rejected_payments",
    "holds",
    "hold_releases",
    "api_requests",
    "retention_runs",
):
    for _op in ("UPDATE", "DELETE"):
        if _op == "DELETE" and _table in RETENTION_COLUMNS:
            # A row may only be deleted once it is older than the table's retention period.
            # Without a policy the cutoff falls back to year 0, so no row is old enough and
            # every delete is refused. A row without a timestamp is never deleted.
            _column = RETENTION_COLUMNS[_table]
            _ddl = _block(
                _table,
                _op,
                when=(
                    f"WHEN OLD.{_column} IS NULL OR OLD.{_column} >= COALESCE((SELECT "  # nosec B608
                    f"datetime('now', '-' || keep_days || ' days') FROM retention_policy "
                    f"WHERE table_name = '{_table}'), '0000-01-01') "
                ),
                message=f"{_table} rows are kept until their retention period ends",
            )
        else:
            _ddl = _block(_table, _op)
        event.listen(Base.metadata.tables[_table], "after_create", _ddl)


def init_db(engine) -> None:
    """Create all tables and the append-only triggers if they do not exist."""
    Base.metadata.create_all(engine)
