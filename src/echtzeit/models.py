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


# Append-only enforcement. Corrections must be new transfers, never edits.
for _table in ("ledger_entries", "transfers", "rejected_payments", "holds", "hold_releases"):
    for _op in ("UPDATE", "DELETE"):
        _ddl = DDL(
            f"CREATE TRIGGER IF NOT EXISTS {_table}_no_{_op.lower()} "
            f"BEFORE {_op} ON {_table} "
            f"BEGIN SELECT RAISE(ABORT, '{_table} is append-only'); END"
        ).execute_if(dialect="sqlite")
        event.listen(Base.metadata.tables[_table], "after_create", _ddl)


def init_db(engine) -> None:
    """Create all tables and the append-only triggers if they do not exist."""
    Base.metadata.create_all(engine)
