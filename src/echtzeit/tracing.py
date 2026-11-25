"""Follow a payment through later transfers and freeze the money that can still be stopped.

See docs/design/holds-and-tracing.md for the model and its limits. In short: the trace
replays, in ledger order, every transfer after the traced payment's credit. Each account has
two pools, traced money and other money. Because cents are interchangeable, the split of a
debit between the pools depends on a rule. Two rules give an upper and a lower bound.

The replay keeps one pair of pools per account, so money that reaches an account twice, or
comes back to an account it left, is never counted twice. The result is a graph of accounts
and hops, not a tree.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from echtzeit import holds
from echtzeit.errors import InvalidRequest, TraceInconsistency, TransferNotFound
from echtzeit.holds import HoldInfo
from echtzeit.ledger import Ledger
from echtzeit.models import SYSTEM, Account, Hold, HoldRelease, LedgerEntry, Transfer

DEFAULT_MAX_DEPTH = 5


class Attribution(Enum):
    """Which money a debit uses when an account holds both traced and other money."""

    OWN_FUNDS_FIRST = "OWN_FUNDS_FIRST"  # other money first: most traced money stays (upper bound)
    TRACED_FUNDS_FIRST = "TRACED_FUNDS_FIRST"  # traced money first: least stays (lower bound)


@dataclass(frozen=True)
class Hop:
    """A transfer that carried traced money from one account to another."""

    transfer_id: str
    from_account_id: str
    to_account_id: str
    traced_cents: int
    depth: int  # depth of the receiving account through this hop


@dataclass
class AccountTrace:
    account_id: str
    account_kind: str
    depth: int  # fewest hops from the first account, among hops replayed so far
    traced_in_cents: int  # all traced money that arrived (for the first account: the payment)
    remaining_cents: int  # traced money still on the account
    secured_cents: int  # part of the remaining money covered by active holds of this payment
    moved_on_cents: int  # traced money that left the account (followed or not)
    unfollowed_cents: int  # traced money that left beyond the depth limit


@dataclass(frozen=True)
class TraceResult:
    uetr: str
    attribution: Attribution
    max_depth: int
    payment_cents: int
    accounts: list[AccountTrace]  # in order of first arrival; the first one received the payment
    hops: list[Hop] = field(default_factory=list)

    @property
    def root(self) -> AccountTrace:
        return self.accounts[0]

    def nodes(self) -> Iterator[AccountTrace]:
        return iter(self.accounts)

    def hops_from(self, account_id: str) -> list[Hop]:
        return [hop for hop in self.hops if hop.from_account_id == account_id]

    @property
    def traced_cents(self) -> int:
        """The amount of the traced payment. Equals remaining plus unfollowed money."""
        return self.payment_cents

    @property
    def remaining_cents(self) -> int:
        return sum(node.remaining_cents for node in self.accounts)

    @property
    def secured_cents(self) -> int:
        return sum(node.secured_cents for node in self.accounts)

    @property
    def unfollowed_cents(self) -> int:
        return sum(node.unfollowed_cents for node in self.accounts)


@dataclass(frozen=True)
class FreezeResult:
    trace: TraceResult  # the trace the holds were based on, taken before the holds
    holds: list[HoldInfo]
    requested_cents: int  # traced money found still on accounts and not yet held
    secured_cents: int  # what the new holds actually secured


def trace_payment(
    ledger: Ledger,
    uetr: str,
    *,
    attribution: Attribution,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> TraceResult:
    """Trace a payment forward. Read-only."""
    with ledger.transaction() as session:
        return _trace(session, uetr, attribution, max_depth)


def freeze_traced_funds(
    ledger: Ledger,
    uetr: str,
    *,
    attribution: Attribution,
    reason: str,
    placed_by: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> FreezeResult:
    """Trace a payment and hold the traced money still on each account, in one transaction.

    Money already held by an earlier freeze of the same payment is not held twice.
    """
    with ledger.transaction() as session:
        trace = _trace(session, uetr, attribution, max_depth)
        placed: list[HoldInfo] = []
        requested = 0
        for node in trace.nodes():
            open_cents = node.remaining_cents - node.secured_cents
            if open_cents <= 0 or node.account_kind == SYSTEM:
                continue
            requested += open_cents
            hold = holds.freeze_available(
                session,
                node.account_id,
                open_cents,
                reason=reason,
                placed_by=placed_by,
                uetr=uetr,
            )
            if hold is not None:
                placed.append(hold)
        return FreezeResult(
            trace=trace,
            holds=placed,
            requested_cents=requested,
            secured_cents=sum(hold.amount_cents for hold in placed),
        )


# -- internals -------------------------------------------------------------------------------


@dataclass
class _Pools:
    kind: str
    other: int
    traced: int = 0
    held_tagged: int = 0  # active holds tagged with the traced payment
    held_other: int = 0  # all other active holds
    node: AccountTrace | None = None  # set once traced money has arrived

    def split_debit(self, debit: int, attribution: Attribution, where: str) -> int:
        """Remove a debit from the pools. Returns the traced part."""
        if self.kind == SYSTEM:
            # System accounts may go negative and never pass traced money on.
            self.other -= debit
            return 0
        pinned_traced = min(self.traced, self.held_tagged)
        pinned_other = min(self.other, self.held_other + (self.held_tagged - pinned_traced))
        spendable_traced = self.traced - pinned_traced
        spendable_other = self.other - pinned_other
        if debit > spendable_traced + spendable_other:
            raise TraceInconsistency(
                f"{where}: debit of {debit} cents exceeds the "
                f"{spendable_traced + spendable_other} cents the replay says were spendable"
            )
        if attribution is Attribution.OWN_FUNDS_FIRST:
            from_other = min(spendable_other, debit)
            from_traced = debit - from_other
        else:
            from_traced = min(spendable_traced, debit)
            from_other = debit - from_traced
        self.traced -= from_traced
        self.other -= from_other
        return from_traced


# Event kinds, ordered so that at the same position a hold applies before its own release.
_TRANSFER, _HOLD_PLACED, _HOLD_RELEASED = 0, 1, 2


def _trace(session: Session, uetr: str, attribution: Attribution, max_depth: int) -> TraceResult:
    if not isinstance(attribution, Attribution):
        raise InvalidRequest("attribution must be an Attribution")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise InvalidRequest("max_depth must be an integer of zero or more")

    payment = session.scalar(select(Transfer).where(Transfer.uetr == uetr))
    if payment is None:
        raise TransferNotFound(uetr)
    start = session.scalar(
        select(LedgerEntry.id).where(
            LedgerEntry.transfer_id == payment.id, LedgerEntry.amount_cents > 0
        )
    )

    # Every account starts with the money it had just before the payment's credit as other
    # money. The receiving account then gets the payment as traced money.
    kinds = dict(session.execute(select(Account.id, Account.kind)).all())
    before = dict(
        session.execute(
            select(LedgerEntry.account_id, func.sum(LedgerEntry.amount_cents))
            .where(LedgerEntry.id < start)
            .group_by(LedgerEntry.account_id)
        ).all()
    )
    pools = {
        account_id: _Pools(kind, before.get(account_id, 0)) for account_id, kind in kinds.items()
    }
    accounts: list[AccountTrace] = []
    hops: list[Hop] = []

    def arrive(account_id: str, cents: int, depth: int) -> None:
        pool = pools[account_id]
        pool.traced += cents
        if pool.node is None:
            pool.node = AccountTrace(account_id, pool.kind, depth, 0, 0, 0, 0, 0)
            accounts.append(pool.node)
        pool.node.depth = min(pool.node.depth, depth)
        pool.node.traced_in_cents += cents

    arrive(payment.creditor_id, payment.amount_cents, 0)

    for _, kind, data in _events(session, start):
        if kind == _HOLD_PLACED or kind == _HOLD_RELEASED:
            account_id, amount, hold_uetr = data
            sign = 1 if kind == _HOLD_PLACED else -1
            if hold_uetr == uetr:
                pools[account_id].held_tagged += sign * amount
            else:
                pools[account_id].held_other += sign * amount
            continue

        transfer_id, debtor_id, creditor_id, amount = data
        sender = pools[debtor_id]
        traced_part = sender.split_debit(amount, attribution, f"transfer {transfer_id}")
        receiver = pools[creditor_id]
        receiver.other += amount - traced_part
        if traced_part == 0:
            continue
        sender.node.moved_on_cents += traced_part
        if sender.node.depth >= max_depth:
            # Beyond the depth limit: the money is no longer followed and counts as other money.
            sender.node.unfollowed_cents += traced_part
            receiver.other += traced_part
            continue
        depth = sender.node.depth + 1
        arrive(creditor_id, traced_part, depth)
        hops.append(Hop(transfer_id, debtor_id, creditor_id, traced_part, depth))

    for node in accounts:
        pool = pools[node.account_id]
        node.remaining_cents = pool.traced
        node.secured_cents = min(pool.traced, max(pool.held_tagged, 0))

    return TraceResult(uetr, attribution, max_depth, payment.amount_cents, accounts, hops)


def _events(session: Session, start: int) -> list[tuple[int, int, tuple]]:
    """Transfers after the start entry and every hold event, in the order they happened.

    A transfer is placed at its credit entry id. A hold records the highest entry id when it
    was placed, so it sorts after every entry that existed then.
    """
    events: list[tuple[int, int, tuple]] = []

    rows = session.execute(
        select(
            Transfer.id,
            Transfer.debtor_id,
            Transfer.creditor_id,
            Transfer.amount_cents,
            func.max(LedgerEntry.id),
        )
        .join(LedgerEntry, LedgerEntry.transfer_id == Transfer.id)
        .group_by(Transfer.id)
        .having(func.min(LedgerEntry.id) > start)
    ).all()
    for transfer_id, debtor_id, creditor_id, amount, last_entry in rows:
        events.append((2 * last_entry, _TRANSFER, (transfer_id, debtor_id, creditor_id, amount)))

    for hold in session.scalars(select(Hold)):
        events.append(
            (
                2 * hold.placed_after_entry_id + 1,
                _HOLD_PLACED,
                (hold.account_id, hold.amount_cents, hold.uetr),
            )
        )
    for release, hold in session.execute(
        select(HoldRelease, Hold).join(Hold, Hold.id == HoldRelease.hold_id)
    ):
        events.append(
            (
                2 * release.released_after_entry_id + 1,
                _HOLD_RELEASED,
                (hold.account_id, hold.amount_cents, hold.uetr),
            )
        )

    events.sort(key=lambda event: (event[0], event[1]))
    return events
