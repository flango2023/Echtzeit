"""Holds: money stopped on an account without moving it.

These functions take an open session so a caller can combine them with other work in one
database transaction (tracing does this). `Ledger` wraps them for single calls.

A hold is an immutable row. It ends with an immutable release row. The cached
`accounts.held_cents` is changed in the same transaction as the row, and
`Ledger.verify_invariants` checks that the cache equals the sum of active holds.
"""

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from echtzeit.errors import AccountNotFound, InsufficientFunds, InvalidRequest
from echtzeit.models import SYSTEM, Account, Hold, HoldRelease, LedgerEntry

MAX_REASON_LENGTH = 200
MAX_ACTOR_LENGTH = 100


@dataclass(frozen=True)
class HoldInfo:
    id: str
    account_id: str
    requested_cents: int
    amount_cents: int
    reason: str
    placed_by: str
    uetr: str | None


def info(hold: Hold) -> HoldInfo:
    return HoldInfo(
        id=hold.id,
        account_id=hold.account_id,
        requested_cents=hold.requested_cents,
        amount_cents=hold.amount_cents,
        reason=hold.reason,
        placed_by=hold.placed_by,
        uetr=hold.uetr,
    )


def _text(label: str, value: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{label} must be a non-empty string")
    if len(value) > limit:
        raise InvalidRequest(f"{label} must be at most {limit} characters")
    return value.strip()


def _positive_int(label: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidRequest(f"{label} must be a positive integer")
    return value


def _last_entry_id(session: Session) -> int:
    return session.scalar(select(func.coalesce(func.max(LedgerEntry.id), 0)))


def place_hold(
    session: Session,
    account_id: str,
    amount_cents: int,
    *,
    reason: str,
    placed_by: str,
    uetr: str | None = None,
    requested_cents: int | None = None,
) -> HoldInfo:
    """Hold an exact amount. Fails if the account cannot cover it with spendable money."""
    amount_cents = _positive_int("amount_cents", amount_cents)
    requested_cents = (
        amount_cents
        if requested_cents is None
        else _positive_int("requested_cents", requested_cents)
    )
    if requested_cents < amount_cents:
        raise InvalidRequest("requested_cents cannot be lower than amount_cents")
    reason = _text("reason", reason, MAX_REASON_LENGTH)
    placed_by = _text("placed_by", placed_by, MAX_ACTOR_LENGTH)

    # The check is part of the UPDATE, so it is atomic with the change.
    result = session.execute(
        update(Account)
        .where(
            Account.id == account_id,
            Account.kind != SYSTEM,
            Account.balance_cents - Account.held_cents >= amount_cents,
        )
        .values(held_cents=Account.held_cents + amount_cents),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        kind = session.scalar(select(Account.kind).where(Account.id == account_id))
        if kind is None:
            raise AccountNotFound(account_id)
        if kind == SYSTEM:
            raise InvalidRequest("a system account cannot be held")
        raise InsufficientFunds(
            f"account {account_id} has less than {amount_cents} cents spendable"
        )

    hold = Hold(
        account_id=account_id,
        requested_cents=requested_cents,
        amount_cents=amount_cents,
        reason=reason,
        placed_by=placed_by,
        uetr=uetr,
        placed_after_entry_id=_last_entry_id(session),
    )
    session.add(hold)
    session.flush()
    return info(hold)


def freeze_available(
    session: Session,
    account_id: str,
    up_to_cents: int,
    *,
    reason: str,
    placed_by: str,
    uetr: str | None = None,
) -> HoldInfo | None:
    """Hold as much as is spendable, up to `up_to_cents`. Returns None if nothing is spendable.

    The hold records both the requested and the secured amount. The read and the update run in
    one transaction, which is safe here because SQLite serializes writers.
    """
    up_to_cents = _positive_int("up_to_cents", up_to_cents)
    row = session.execute(
        select(Account.kind, Account.balance_cents, Account.held_cents).where(
            Account.id == account_id
        )
    ).one_or_none()
    if row is None:
        raise AccountNotFound(account_id)
    kind, balance, held = row
    if kind == SYSTEM:
        raise InvalidRequest("a system account cannot be held")
    secured = min(up_to_cents, balance - held)
    if secured <= 0:
        return None
    return place_hold(
        session,
        account_id,
        secured,
        reason=reason,
        placed_by=placed_by,
        uetr=uetr,
        requested_cents=up_to_cents,
    )


def release_hold(session: Session, hold_id: str, *, reason: str, released_by: str) -> None:
    reason = _text("reason", reason, MAX_REASON_LENGTH)
    released_by = _text("released_by", released_by, MAX_ACTOR_LENGTH)
    hold = session.get(Hold, hold_id)
    if hold is None:
        raise InvalidRequest(f"unknown hold {hold_id!r}")
    if session.get(HoldRelease, hold_id) is not None:
        raise InvalidRequest(f"hold {hold_id!r} is already released")
    session.execute(
        update(Account)
        .where(Account.id == hold.account_id)
        .values(held_cents=Account.held_cents - hold.amount_cents),
        execution_options={"synchronize_session": False},
    )
    session.add(
        HoldRelease(
            hold_id=hold_id,
            reason=reason,
            released_by=released_by,
            released_after_entry_id=_last_entry_id(session),
        )
    )
    session.flush()


def active_holds(session: Session, account_id: str) -> list[HoldInfo]:
    statement = (
        select(Hold)
        .outerjoin(HoldRelease, HoldRelease.hold_id == Hold.id)
        .where(Hold.account_id == account_id, HoldRelease.hold_id.is_(None))
        .order_by(Hold.placed_after_entry_id, Hold.placed_at)
    )
    return [info(hold) for hold in session.scalars(statement)]
