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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    holder_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default=CUSTOMER)
    # Payment identifiers. Only customer accounts have them; the system account has neither.
    iban: Mapped[str | None] = mapped_column(String(34), unique=True, nullable=True)
    bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    # Cached balance. The ledger is the source of truth; verify_invariants checks both agree.
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)
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
    uetr: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (CheckConstraint("amount_cents <> 0", name="ck_entry_nonzero"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


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
for _table in ("ledger_entries", "transfers", "rejected_payments"):
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
