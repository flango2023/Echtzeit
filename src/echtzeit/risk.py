"""Transparent, rule-based risk score for an outgoing instant payment.

The score is computed on the sending side before the payment is made, as an instant payment
cannot be recalled afterwards. Every rule adds points and a readable reason. The total gives
one of three decisions: allow, warn (the payer is told and decides) or block.

The signals are generic and widely published (new payee, unusual amount, payee-name check,
money passing straight through an account). No rule here is secret or tuned to evade.

The points and thresholds are this project's choices. They are not calibrated on real data,
and the scenarios that exercise them were written by the same author. Results from those
scenarios therefore measure the rules against assumptions, not real fraud.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from echtzeit.ledger import Ledger
from echtzeit.models import SYSTEM, Account, Transfer, to_db_time
from echtzeit.vop import VopOutcome, VopResult, compare_names, verify_payee


class Decision(Enum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class RiskConfig:
    """All points and thresholds in one place, so a report can print them."""

    new_payee: int = 20
    amount_spike: int = 25
    amount_spike_factor: int = 3  # amount above this many times the largest earlier payment
    amount_spike_min_cents: int = 50_000
    first_large_payment_min_cents: int = 100_000  # no earlier payments and at least this much
    balance_drain: int = 20
    balance_drain_share: float = 0.8
    balance_drain_min_cents: int = 50_000
    vop_no_match: int = 40
    vop_close_match: int = 10
    vop_not_possible: int = 15
    fresh_receiver: int = 15
    pass_through: int = 35
    pass_through_window: timedelta = timedelta(minutes=60)
    pass_through_share: float = 0.8
    warn_at: int = 40
    block_at: int = 70


@dataclass(frozen=True)
class Reason:
    rule: str
    points: int
    detail: str


@dataclass(frozen=True)
class Assessment:
    decision: Decision
    points: int
    reasons: list[Reason]
    vop: VopResult
    elapsed_ns: int  # time spent on the assessment, VoP included

    def fired(self, rule: str) -> bool:
        return any(reason.rule == rule for reason in self.reasons)


@dataclass
class _Features:
    payee_known: bool = False
    largest_earlier_cents: int | None = None
    available_cents: int = 0
    receiver_credit_count: int = 0
    recent_customer_credits_cents: int = 0


class RiskScorer:
    def __init__(self, ledger: Ledger, config: RiskConfig | None = None):
        self._ledger = ledger
        self.config = config or RiskConfig()

    def assess(
        self,
        *,
        debtor_id: str,
        creditor_iban: str,
        typed_name: str,
        amount_cents: int,
        at: datetime,
    ) -> Assessment:
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValueError("amount_cents must be a positive whole number")
        started = time.perf_counter_ns()
        cfg = self.config
        vop = verify_payee(self._ledger, creditor_iban, typed_name)
        creditor = self._ledger.find_account_by_iban(creditor_iban)
        features = self._features(debtor_id, creditor.id if creditor else None, at)
        reasons: list[Reason] = []

        def add(rule: str, points: int, detail: str) -> None:
            reasons.append(Reason(rule, points, detail))

        if not features.payee_known:
            add("NEW_PAYEE", cfg.new_payee, "first payment from this account to this payee")

        largest = features.largest_earlier_cents
        if largest is None:
            if amount_cents >= cfg.first_large_payment_min_cents:
                add("AMOUNT_SPIKE", cfg.amount_spike, "large amount and no earlier payments")
        elif (
            amount_cents >= cfg.amount_spike_min_cents
            and amount_cents > cfg.amount_spike_factor * largest
        ):
            add(
                "AMOUNT_SPIKE",
                cfg.amount_spike,
                f"more than {cfg.amount_spike_factor} times the largest earlier payment",
            )

        if (
            amount_cents >= cfg.balance_drain_min_cents
            and features.available_cents > 0
            and amount_cents >= cfg.balance_drain_share * features.available_cents
        ):
            add("BALANCE_DRAIN", cfg.balance_drain, "moves most of the spendable balance")

        if vop.outcome is VopOutcome.NO_MATCH:
            add("VOP_NO_MATCH", cfg.vop_no_match, "payee name does not match the account")
        elif vop.outcome is VopOutcome.CLOSE_MATCH:
            add("VOP_CLOSE_MATCH", cfg.vop_close_match, "payee name is close to the account name")
        elif vop.outcome is VopOutcome.NOT_POSSIBLE:
            add("VOP_NOT_POSSIBLE", cfg.vop_not_possible, "payee name could not be checked")

        if creditor is not None and features.receiver_credit_count == 0:
            add("FRESH_RECEIVER", cfg.fresh_receiver, "the receiving account has never been paid")

        if features.recent_customer_credits_cents >= cfg.pass_through_share * amount_cents:
            add(
                "PASS_THROUGH",
                cfg.pass_through,
                "most of this amount arrived from other customers within the last "
                f"{int(cfg.pass_through_window.total_seconds() // 60)} minutes",
            )

        points = sum(reason.points for reason in reasons)
        if points >= cfg.block_at:
            decision = Decision.BLOCK
        elif points >= cfg.warn_at:
            decision = Decision.WARN
        else:
            decision = Decision.ALLOW
        return Assessment(decision, points, reasons, vop, time.perf_counter_ns() - started)

    def _features(self, debtor_id: str, creditor_id: str | None, at: datetime) -> _Features:
        moment = to_db_time(at)
        since = moment - self.config.pass_through_window
        features = _Features()
        sender = aliased(Account)
        with self._ledger.transaction() as session:
            features.available_cents = session.scalar(
                select(Account.balance_cents - Account.held_cents).where(Account.id == debtor_id)
            )
            features.largest_earlier_cents = session.scalar(
                select(func.max(Transfer.amount_cents)).where(
                    Transfer.debtor_id == debtor_id, Transfer.created_at < moment
                )
            )
            features.recent_customer_credits_cents = session.scalar(
                select(func.coalesce(func.sum(Transfer.amount_cents), 0))
                .join(sender, sender.id == Transfer.debtor_id)
                .where(
                    Transfer.creditor_id == debtor_id,
                    Transfer.created_at >= since,
                    Transfer.created_at <= moment,
                    sender.kind != SYSTEM,
                )
            )
            if creditor_id is not None:
                features.payee_known = (
                    session.scalar(
                        select(func.count(Transfer.id)).where(
                            Transfer.debtor_id == debtor_id,
                            Transfer.creditor_id == creditor_id,
                            Transfer.created_at < moment,
                        )
                    )
                    > 0
                )
                features.receiver_credit_count = session.scalar(
                    select(func.count(Transfer.id)).where(
                        Transfer.creditor_id == creditor_id, Transfer.created_at < moment
                    )
                )
        return features


def recent_incoming_uetrs(
    ledger: Ledger, account_id: str, *, at: datetime, window: timedelta
) -> list[tuple[str, int]]:
    """Payments from other customers that reached the account within the window, newest first.

    A receiving bank that blocks a pass-through payment uses this to freeze the money it
    suspects, tagged with the UETR of each incoming payment.
    """
    moment = to_db_time(at)
    sender = aliased(Account)
    with ledger.transaction() as session:
        rows = session.execute(
            select(Transfer.uetr, Transfer.amount_cents)
            .join(sender, sender.id == Transfer.debtor_id)
            .where(
                Transfer.creditor_id == account_id,
                Transfer.created_at >= moment - window,
                Transfer.created_at <= moment,
                Transfer.uetr.is_not(None),
                sender.kind != SYSTEM,
            )
            .order_by(Transfer.created_at.desc())
        ).all()
    return [(uetr, amount) for uetr, amount in rows]


# -- receiving side ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncomingConfig:
    """Rules the payee's bank applies before crediting an incoming payment. Not calibrated."""

    name_mismatch: int = 30
    first_credit: int = 25
    large_first_credit: int = 20
    large_credit_factor: int = 3  # above this many times the largest earlier credit
    large_credit_min_cents: int = 100_000
    hold_at: int = 45


# Rules that are about the payee's name only. EU PSR art. 73(3) (Council position at first
# reading) does not allow the decision to rest solely on the payee-name check.
_NAME_ONLY = frozenset({"NAME_MISMATCH"})


@dataclass(frozen=True)
class IncomingAssessment:
    hold_back: bool
    points: int
    reasons: list[Reason]

    def fired(self, rule: str) -> bool:
        return any(reason.rule == rule for reason in self.reasons)


class IncomingScreen:
    """Screens a payment before the payee's bank makes the money available.

    A payment is held back (and returned to the payer's bank) when the points reach `hold_at`
    and at least one rule other than the name check fired.
    """

    def __init__(self, ledger: Ledger, config: IncomingConfig | None = None):
        self._ledger = ledger
        self.config = config or IncomingConfig()

    def assess(
        self, *, creditor_id: str, named_creditor: str, amount_cents: int, at: datetime
    ) -> IncomingAssessment:
        cfg = self.config
        moment = to_db_time(at)
        creditor = self._ledger.account_info(creditor_id)
        sender = aliased(Account)
        with self._ledger.transaction() as session:
            earlier = session.execute(
                select(func.count(Transfer.id), func.max(Transfer.amount_cents))
                .join(sender, sender.id == Transfer.debtor_id)
                .where(
                    Transfer.creditor_id == creditor_id,
                    Transfer.created_at < moment,
                    sender.kind != SYSTEM,
                )
            ).one()
        count, largest = earlier
        reasons: list[Reason] = []
        outcome, _ = compare_names(named_creditor, creditor.holder_name)
        if outcome is VopOutcome.NO_MATCH:
            reasons.append(
                Reason(
                    "NAME_MISMATCH",
                    cfg.name_mismatch,
                    "the payer named someone other than the account holder",
                )
            )
        if count == 0:
            reasons.append(
                Reason(
                    "FIRST_CREDIT",
                    cfg.first_credit,
                    "first payment from another customer to this account",
                )
            )
        if amount_cents >= cfg.large_credit_min_cents and (
            largest is None or amount_cents > cfg.large_credit_factor * largest
        ):
            reasons.append(
                Reason(
                    "LARGE_FIRST_CREDIT",
                    cfg.large_first_credit,
                    "much larger than any earlier payment to this account",
                )
            )
        points = sum(reason.points for reason in reasons)
        beyond_name = any(reason.rule not in _NAME_ONLY for reason in reasons)
        return IncomingAssessment(points >= cfg.hold_at and beyond_name, points, reasons)
