"""Synthetic scam scenarios run through the whole payment flow.

Every payment between customers goes: Verification of Payee and risk score (sending side),
then a pacs.008 message, then the processor and the ledger. Scam money then moves through mule
accounts and is withdrawn as cash. When the victim reports the scam, the payment is traced
and the traced money still on accounts is frozen.

The same population and schedule run under four policies:

* NONE: no risk score. Only the freeze after the victim's report.
* SENDER_ONLY: the sending side scores every payment and warns or blocks.
* SENDER_AND_RECEIVER: as SENDER_ONLY, and when an outgoing payment is blocked because money is
  passing straight through an account, that bank also freezes the money that just arrived.
  The bank acts when the money tries to leave. This differs from EU PSR art. 73(3) (Council
  position at first reading), under which the payee's PSP may hold back suspicious funds when
  they arrive.
* FULL_CHAIN: as SENDER_AND_RECEIVER, and the payee's bank also screens every incoming payment
  before crediting it (echtzeit.risk.IncomingScreen). A payment it holds back is rejected and
  nothing moves. This is the only policy in which both banks monitor, as EU art. 87(3) asks.

Scenario patterns, paraphrased from the EPC 2025 Payments Threats and Fraud Trends Report
(EPC162-24, version 2.0, 19 November 2025):

* Safe account (section 2, page 17): a fraudster posing as the bank convinces the customer
  that the account is at risk and that the money must be moved to a "safe" account the
  fraudster controls.
* Invoice with a changed IBAN (executive summary, page 7: the invoicing stage is exposed to
  IBAN manipulation). The invoice keeps the real supplier's name but carries the IBAN of an
  account the fraudster controls.
* Money mules (section 3.1.7.1, page 44): mule accounts receive the money and pass it on so that
  it cannot be tracked, often ending in a cash withdrawal. The report notes that most cases
  involve no more than one or two levels of mules, which is the range used here.

Every number in ScenarioConfig (behaviour probabilities, delays, amounts) is an assumption of
this project, not a measured value. The scenarios and the risk rules were written by the same
author, so the results show how the rules behave against these assumptions. They are not
evidence about real fraud or real detection rates.
"""

import hashlib
import heapq
import math
import random
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path

from echtzeit import holds
from echtzeit.audit import AuditLog
from echtzeit.db import make_engine, make_session_factory
from echtzeit.errors import InsufficientFunds
from echtzeit.iso20022 import pacs008
from echtzeit.iso20022.identifiers import make_german_iban, new_uetr
from echtzeit.ledger import Ledger
from echtzeit.models import SYSTEM, init_db
from echtzeit.payments import HELD_BACK, InstantPaymentProcessor
from echtzeit.risk import (
    Assessment,
    Decision,
    IncomingScreen,
    RiskConfig,
    RiskScorer,
    recent_incoming_uetrs,
)
from echtzeit.tracing import Attribution, freeze_traced_funds, trace_payment

BANKS = ("ECHTDEFF", "ECHTDEF1", "ECHTDEF2")

FIRST_NAMES = (
    "Anna", "Ben", "Clara", "David", "Emma", "Felix", "Greta", "Hannah", "Jonas", "Lena",
    "Lukas", "Marie", "Noah", "Paul", "Sophie", "Tim", "Julia", "Elias", "Mia", "Leon",
)  # fmt: skip
SURNAMES = (
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
    "Schulz", "Hoffmann", "Koch", "Richter", "Klein", "Wolf", "Neumann", "Schwarz",
)  # fmt: skip


class Policy(Enum):
    NONE = "NONE"
    SENDER_ONLY = "SENDER_ONLY"
    SENDER_AND_RECEIVER = "SENDER_AND_RECEIVER"
    FULL_CHAIN = "FULL_CHAIN"


class Actor(Enum):
    CUSTOMER = "CUSTOMER"  # a normal customer making its own payment
    VICTIM = "VICTIM"  # a customer being manipulated by a fraudster
    MULE = "MULE"  # a mule following the fraudster's instructions


@dataclass(frozen=True)
class ScenarioConfig:
    """Size of the synthetic world and every behavioural assumption. None of it is measured."""

    seed: int = 7
    start: datetime = datetime(2026, 1, 5, tzinfo=UTC)
    warmup_days: int = 30  # history only: no scoring, no scams, no statistics
    days: int = 30  # measured period
    consumers: int = 40
    businesses: int = 6
    merchants: int = 8
    landlords: int = 5
    safe_account_cases: int = 12
    invoice_cases: int = 12
    # Share of the simulated businesses that are microenterprises (fewer than 10 staff and at
    # most EUR 2 million turnover). Decides whether UK reimbursement covers an invoice scam.
    microenterprise_share: float = 0.5
    # Behaviour when the bank shows a warning.
    customer_ignores_warning: float = 0.9
    victim_ignores_warning: float = 0.6
    # Fraud patterns.
    fraudster_gives_mule_name: float = 0.5  # otherwise the victim types a bank-like name
    mule_is_established: float = 0.5  # otherwise the mule account has never been used
    second_mule_level: float = 0.5
    # Minute ranges are half-open: (2, 30) draws 2 to 29 minutes.
    mule_forward_minutes: tuple[int, int] = (2, 30)
    cash_out_minutes: tuple[int, int] = (5, 90)
    report_delays: tuple[timedelta, ...] = (
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
        timedelta(hours=72),
    )
    # Normal payment habits (probabilities per day).
    p_shop: float = 0.5
    p_friend: float = 0.08
    p_new_merchant: float = 0.02
    p_large_purchase: float = 0.004
    p_typo: float = 0.04  # payer types the name with one wrong letter
    p_trading_name: float = 0.05  # payer types a merchant's trading name, not its legal name
    # Legitimate payments that look like scams: a relative sends a large first payment to a
    # family member's account opened during the measured period. Without them, no normal payee
    # would ever be new, and receiving-side rules about first payments could never misfire.
    new_account_gifts: int = 6
    # None: scam amounts as described above. A value: every scam amount is drawn uniformly
    # between half and one and a half times this mean (a safe-account victim never sends more
    # than the available balance). Used for a sensitivity run with the EBA-ECB average value of
    # a fraudulent credit transfer; the shape of the distribution is an assumption.
    scam_amount_mean_cents: int | None = None


@dataclass
class CaseOutcome:
    case_id: str
    kind: str  # SAFE_ACCOUNT or INVOICE_IBAN_CHANGE
    victim_id: str
    victim_type: str  # CONSUMER, MICROENTERPRISE or OTHER_BUSINESS (see echtzeit.liability)
    attempted_cents: int
    decision: str | None  # None when the policy does not score
    points: int | None
    reasons: list[str]
    vop: str | None
    warned_and_ignored: bool
    executed: bool
    uetr: str | None = None
    report_delay: timedelta | None = None
    remaining_upper_cents: int = 0  # traced money still on accounts at the report, upper bound
    remaining_lower_cents: int = 0  # the same, lower bound
    recovered_cents: int = 0  # stopped by holds along the traced path (lower-bound rule)
    # The part of it stopped on the first receiving account (the first mule). Regimes without
    # system-wide tracing are measured with this figure; see echtzeit.liability.
    recovered_first_hop_cents: int = 0
    mule_levels: int = 0
    receiver_screened: bool = False  # the payee's bank screened the incoming payment
    held_back: bool = False  # the payee's bank held it back before crediting

    @property
    def lost_cents(self) -> int:
        return self.attempted_cents - self.recovered_cents if self.executed else 0

    @property
    def prevented_cents(self) -> int:
        return 0 if self.executed else self.attempted_cents


@dataclass
class LegitStats:
    payments: int = 0
    allowed: int = 0
    warned: int = 0
    warned_and_went_ahead: int = 0
    blocked: int = 0
    refused_by_bank: int = 0  # not enough money, for example
    held_back_by_receiver: int = 0  # screened by the payee's bank and returned
    vop: dict[str, int] = field(default_factory=dict)

    @property
    def flagged_share(self) -> float:
        return (self.warned + self.blocked) / self.payments if self.payments else 0.0


@dataclass
class SimulationResult:
    policy: Policy
    config: ScenarioConfig
    risk_config: RiskConfig
    cases: list[CaseOutcome]
    legit: LegitStats
    assessment_ms: list[float]
    receiver_freezes: int
    invariants_ok: bool

    def total(self, attribute: str) -> int:
        return sum(getattr(case, attribute) for case in self.cases)

    def latency_percentile(self, percent: float) -> float | None:
        return percentile(self.assessment_ms, percent)


def percentile(values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile. None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(percent / 100 * len(ordered))
    return ordered[min(len(ordered), max(1, rank)) - 1]


def _draw(seed: int, *keys: object) -> float:
    """A number in [0, 1) that depends only on the seed and the keys, never on the policy.

    Decisions that differ between policies (for example whether a warned victim goes ahead)
    use this, so the three policies face the same choices.
    """
    digest = hashlib.sha256("|".join(map(str, (seed, *keys))).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True)
class _Party:
    id: str
    name: str
    iban: str
    bic: str


class _Run:
    def __init__(
        self, config: ScenarioConfig, policy: Policy, risk_config: RiskConfig, db_url: str
    ):
        self.cfg = config
        self.policy = policy
        engine = make_engine(db_url)
        init_db(engine)
        self.engine = engine
        sessions = make_session_factory(engine)
        self.ledger = Ledger(sessions)
        self.now = config.start - timedelta(days=config.warmup_days + 1)
        self.incoming = IncomingScreen(self.ledger) if policy is Policy.FULL_CHAIN else None
        self.processor = InstantPaymentProcessor(
            self.ledger,
            audit=AuditLog(sessions),
            clock=lambda: self.now,
            incoming_screen=self.screen_incoming if self.incoming else None,
        )
        self.scorer = RiskScorer(self.ledger, risk_config)
        self.last_reason: str | None = None
        # Seeded on purpose, for reproducible simulations. Nothing security-related uses it:
        # payment identifiers (UETR) come from uuid4, which uses the operating system's source.
        self.rng = random.Random(config.seed)  # nosec B311
        self.queue: list[tuple[datetime, int, Callable[[], None]]] = []
        self.seq = 0
        self.keys = 0
        self.funding = self.ledger.create_account("Echtzeit Funding", kind=SYSTEM)
        self.cash = self.ledger.create_account("Cash withdrawals", kind=SYSTEM)
        self.accounts = 0
        self.cases: list[CaseOutcome] = []
        self.legit = LegitStats()
        self.latency: list[float] = []
        self.receiver_freezes = 0

    # -- plumbing ----------------------------------------------------------------------------

    def at(self, moment: datetime, action: Callable[[], None]) -> None:
        self.seq += 1
        heapq.heappush(self.queue, (moment, self.seq, action))

    def key(self, prefix: str) -> str:
        self.keys += 1
        return f"{prefix}-{self.keys}"

    def open_account(self, name: str) -> _Party:
        self.accounts += 1
        bic = BANKS[self.accounts % len(BANKS)]
        iban = make_german_iban(str(self.accounts))
        return _Party(self.ledger.create_account(name, iban=iban, bic=bic), name, iban, bic)

    def fund(self, party: _Party, cents: int, moment: datetime) -> None:
        self.ledger.transfer(
            idempotency_key=self.key("fund"),
            debtor_id=self.funding,
            creditor_id=party.id,
            amount_cents=cents,
            booked_at=moment,
        )

    def person_name(self) -> str:
        return f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(SURNAMES)}"

    def measured(self, moment: datetime) -> bool:
        return moment >= self.cfg.start

    # -- one instant payment -----------------------------------------------------------------

    def instant_payment(
        self,
        debtor: _Party,
        creditor: _Party,
        typed_name: str,
        cents: int,
        actor: Actor,
        draw_key: tuple,
    ) -> tuple[Assessment | None, str | None, bool]:
        """Score and send one payment. Returns the assessment, the UETR if it was made, and
        whether a warning was shown and ignored."""
        # Reset first: a payment that is blocked or declined never reaches the processor, and
        # must not inherit the reason code of the payment before it.
        self.last_reason = None
        assessment = None
        ignored_warning = False
        if self.policy is not Policy.NONE and self.measured(self.now):
            assessment = self.scorer.assess(
                debtor_id=debtor.id,
                creditor_iban=creditor.iban,
                typed_name=typed_name,
                amount_cents=cents,
                at=self.now,
            )
            self.latency.append(assessment.elapsed_ns / 1e6)
            if assessment.decision is Decision.BLOCK:
                if self.policy in (
                    Policy.SENDER_AND_RECEIVER,
                    Policy.FULL_CHAIN,
                ) and assessment.fired("PASS_THROUGH"):
                    self.freeze_incoming(debtor)
                return assessment, None, False
            if assessment.decision is Decision.WARN:
                ignore = {
                    Actor.CUSTOMER: self.cfg.customer_ignores_warning,
                    Actor.VICTIM: self.cfg.victim_ignores_warning,
                    Actor.MULE: 1.0,
                }[actor]
                if _draw(self.cfg.seed, *draw_key, "warning") >= ignore:
                    return assessment, None, False
                ignored_warning = True

        uetr = new_uetr()
        self.keys += 1
        tx = f"TX{self.keys}"
        message = pacs008.CreditTransfer(
            msg_id=f"MSG{self.keys}",
            created_at=self.now,
            end_to_end_id=f"E2E{self.keys}",
            tx_id=tx,
            uetr=uetr,
            amount_cents=cents,
            settlement_date=date(self.now.year, self.now.month, self.now.day),
            accepted_at=self.now,
            debtor=pacs008.Party(debtor.name, debtor.iban, debtor.bic),
            creditor=pacs008.Party(typed_name[:70], creditor.iban, creditor.bic),
        )
        result = self.processor.process_transfer(message)
        self.last_reason = result.report.reason_code
        return assessment, (uetr if result.accepted else None), ignored_warning

    def screen_incoming(self, message: pacs008.CreditTransfer, creditor, now: datetime) -> bool:
        """The payee's bank checks a payment before crediting it (FULL_CHAIN, measured period)."""
        if not self.measured(now):
            return False
        return self.incoming.assess(
            creditor_id=creditor.id,
            named_creditor=message.creditor.name,
            amount_cents=message.amount_cents,
            at=now,
        ).hold_back

    def freeze_incoming(self, account: _Party) -> None:
        """Receiving-side freeze: hold what just arrived from other customers, tagged by payment."""
        window = self.scorer.config.pass_through_window
        incoming = recent_incoming_uetrs(self.ledger, account.id, at=self.now, window=window)
        with self.ledger.transaction() as session:
            for uetr, cents in incoming:
                hold = holds.freeze_available(
                    session,
                    account.id,
                    cents,
                    reason="outgoing payment blocked: money passing straight through",
                    placed_by=f"bank {account.bic}",
                    uetr=uetr,
                )
                if hold is not None:
                    self.receiver_freezes += 1

    # -- normal traffic ----------------------------------------------------------------------

    def legit_payment(
        self, debtor: _Party, creditor: _Party, cents: int, day: int, n: int
    ) -> Callable[[], None]:
        typed = creditor.name
        roll = self.rng.random()
        if roll < self.cfg.p_typo:
            position = self.rng.randrange(len(typed))
            typed = (
                typed[:position] + ("x" if typed[position] != "x" else "y") + typed[position + 1 :]
            )
        elif roll < self.cfg.p_typo + self.cfg.p_trading_name and typed.endswith(" GmbH"):
            typed = typed.removesuffix(" GmbH")

        def run() -> None:
            assessment, uetr, ignored = self.instant_payment(
                debtor, creditor, typed, cents, Actor.CUSTOMER, ("legit", day, n)
            )
            if not self.measured(self.now):
                return
            stats = self.legit
            stats.payments += 1
            decision = assessment.decision if assessment else Decision.ALLOW
            if assessment is not None:
                outcome = assessment.vop.outcome.value
                stats.vop[outcome] = stats.vop.get(outcome, 0) + 1
            if decision is Decision.BLOCK:
                stats.blocked += 1
                return
            if decision is Decision.WARN:
                stats.warned += 1
                if not ignored:
                    return  # the customer decided not to pay
                stats.warned_and_went_ahead += 1
            else:
                stats.allowed += 1
            if uetr is None and self.last_reason == HELD_BACK:
                stats.held_back_by_receiver += 1
            elif uetr is None:
                stats.refused_by_bank += 1

        return run

    def build_world(self) -> None:
        cfg, rng = self.cfg, self.rng
        first = self.now
        merchants = [
            self.open_account(f"{name} Markt GmbH")
            for name in ("Bio", "Stadt", "Frisch", "Nord", "Sued", "Tech", "Mode", "Buch")[
                : cfg.merchants
            ]
        ]
        landlords = [self.open_account(self.person_name()) for _ in range(cfg.landlords)]
        self.consumers = [self.open_account(self.person_name()) for _ in range(cfg.consumers)]
        self.businesses = [
            self.open_account(f"Firma {i + 1} Example GmbH") for i in range(cfg.businesses)
        ]
        self.suppliers = {
            business.id: [
                self.open_account(f"Lieferant {i + 1}{j + 1} Example GmbH") for j in range(2)
            ]
            for i, business in enumerate(self.businesses)
        }

        total_days = cfg.warmup_days + cfg.days
        day0 = cfg.start - timedelta(days=cfg.warmup_days)
        n = 0
        for consumer in self.consumers:
            self.fund(consumer, rng.randint(50_000, 800_000), first)
            favourites = rng.sample(merchants, 3)
            landlord = rng.choice(landlords)
            salary = rng.randint(180_000, 450_000)
            rent = rng.randint(50_000, 140_000)
            for day in range(total_days):
                moment = day0 + timedelta(days=day)
                if day % 30 == 0:
                    self.at(
                        moment + timedelta(hours=8),
                        lambda c=consumer, s=salary, m=moment: self.fund(
                            c, s, m + timedelta(hours=8)
                        ),
                    )
                if day % 30 == 1:
                    n += 1
                    self.at(
                        moment + timedelta(hours=9),
                        self.legit_payment(consumer, landlord, rent, day, n),
                    )
                if rng.random() < cfg.p_shop:
                    n += 1
                    self.at(
                        moment + timedelta(hours=rng.randint(9, 20), minutes=rng.randint(0, 59)),
                        self.legit_payment(
                            consumer, rng.choice(favourites), rng.randint(500, 12_000), day, n
                        ),
                    )
                if rng.random() < cfg.p_friend:
                    n += 1
                    friend = rng.choice([c for c in self.consumers if c is not consumer])
                    self.at(
                        moment + timedelta(hours=rng.randint(9, 22)),
                        self.legit_payment(consumer, friend, rng.randint(1_000, 20_000), day, n),
                    )
                if rng.random() < cfg.p_new_merchant:
                    n += 1
                    self.at(
                        moment + timedelta(hours=rng.randint(9, 22)),
                        self.legit_payment(
                            consumer, rng.choice(merchants), rng.randint(2_000, 40_000), day, n
                        ),
                    )
                if rng.random() < cfg.p_large_purchase:
                    n += 1
                    self.at(
                        moment + timedelta(hours=rng.randint(9, 18)),
                        self.legit_payment(
                            consumer, rng.choice(merchants), rng.randint(200_000, 900_000), day, n
                        ),
                    )

        # Accounts opened during the measured period, each receiving one large first payment
        # from a relative. Drawn from the shared generator here, before any policy runs.
        for i in range(cfg.new_account_gifts):
            relative = rng.choice(self.consumers)
            newcomer = self.open_account(self.person_name())
            moment = cfg.start + timedelta(
                days=rng.randrange(cfg.days), hours=rng.randint(9, 20), minutes=rng.randint(0, 59)
            )
            n += 1
            self.at(
                moment,
                self.legit_payment(relative, newcomer, rng.randint(100_000, 300_000), -1, n),
            )

        for business in self.businesses:
            self.fund(business, rng.randint(2_000_000, 8_000_000), first)
            invoices = {
                supplier.id: rng.randint(80_000, 600_000)
                for supplier in self.suppliers[business.id]
            }
            for day in range(total_days):
                moment = day0 + timedelta(days=day)
                if day % 7 == 0:
                    income = rng.randint(500_000, 1_500_000)
                    self.at(
                        moment + timedelta(hours=7),
                        lambda b=business, c=income, m=moment: self.fund(
                            b, c, m + timedelta(hours=7)
                        ),
                    )
                for supplier in self.suppliers[business.id]:
                    if day % 7 == 2:
                        n += 1
                        base = invoices[supplier.id]
                        self.at(
                            moment + timedelta(hours=10),
                            self.legit_payment(
                                business,
                                supplier,
                                rng.randint(int(base * 0.7), int(base * 1.3)),
                                day,
                                n,
                            ),
                        )

    # -- scams -------------------------------------------------------------------------------

    def new_mule(self, case_id: str, level: int) -> _Party:
        # A generator of its own, so creating a mule never shifts the draws of the shared one.
        # (A second-level mule exists only if the first payment went through, which differs
        # between policies.) String seeds are hashed with SHA-512, so this is reproducible.
        rng = random.Random(f"{self.cfg.seed}-{case_id}-{level}")  # nosec B311
        mule = self.open_account(f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}")
        if _draw(self.cfg.seed, case_id, level, "established") < self.cfg.mule_is_established:
            history = self.cfg.start - timedelta(days=20)
            self.fund(mule, rng.randint(10_000, 150_000), history)
            # An earlier payment from another customer makes the account look established. The
            # payer is an account of its own, so it never depends on a real customer's balance
            # (an earlier version took a random customer, who could have run out of money).
            payer = self.open_account(f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}")
            self.fund(payer, 1_000, history)
            self.ledger.transfer(
                idempotency_key=self.key("mule-history"),
                debtor_id=payer.id,
                creditor_id=mule.id,
                amount_cents=1_000,
                booked_at=history + timedelta(hours=1),
            )
        return mule

    def _calibrated_amount(self, rng: random.Random) -> int | None:
        """A scam amount around the configured mean, or None when amounts are not calibrated.

        Draws nothing from the generator when not calibrated, so the default world is unchanged.
        """
        mean = self.cfg.scam_amount_mean_cents
        if mean is None:
            return None
        return rng.randint(max(1, mean // 2), max(1, mean * 3 // 2))

    def build_scams(self) -> None:
        cfg, rng = self.cfg, self.rng
        victims = rng.sample(self.consumers, cfg.safe_account_cases)
        for i, victim in enumerate(victims):
            case_id = f"S{i + 1}"
            moment = cfg.start + timedelta(
                days=rng.randrange(cfg.days), hours=rng.randint(9, 19), minutes=rng.randint(0, 59)
            )
            mule = self.new_mule(case_id, 1)
            share = rng.uniform(0.5, 1.0)
            target = self._calibrated_amount(rng)
            if _draw(cfg.seed, case_id, "name") < cfg.fraudster_gives_mule_name:
                typed = mule.name
            else:
                typed = "Sicherheitskonto Kundenservice"
            self.at(
                moment,
                self.scam_payment(
                    case_id,
                    "SAFE_ACCOUNT",
                    "CONSUMER",
                    victim,
                    mule,
                    typed,
                    share=share,
                    target_cents=target,
                ),
            )

        for i in range(cfg.invoice_cases):
            case_id = f"I{i + 1}"
            business = self.businesses[i % len(self.businesses)]
            supplier = rng.choice(self.suppliers[business.id])
            moment = cfg.start + timedelta(
                days=rng.randrange(cfg.days), hours=rng.randint(9, 17), minutes=rng.randint(0, 59)
            )
            mule = self.new_mule(case_id, 1)
            cents = self._calibrated_amount(rng) or rng.randint(80_000, 600_000)
            micro = _draw(cfg.seed, "microenterprise", business.name) < cfg.microenterprise_share
            victim_type = "MICROENTERPRISE" if micro else "OTHER_BUSINESS"
            self.at(
                moment,
                self.scam_payment(
                    case_id,
                    "INVOICE_IBAN_CHANGE",
                    victim_type,
                    business,
                    mule,
                    supplier.name,
                    cents=cents,
                ),
            )

    def scam_payment(
        self,
        case_id: str,
        kind: str,
        victim_type: str,
        victim: _Party,
        mule: _Party,
        typed: str,
        *,
        share: float | None = None,
        cents: int | None = None,
        target_cents: int | None = None,
    ) -> Callable[[], None]:
        def run() -> None:
            amount = cents
            if amount is None and target_cents is not None:
                amount = max(1, min(target_cents, self.ledger.available_balance(victim.id)))
            elif amount is None:
                amount = max(1, int(self.ledger.available_balance(victim.id) * share))
            assessment, uetr, ignored = self.instant_payment(
                victim, mule, typed, amount, Actor.VICTIM, (case_id, "victim")
            )
            case = CaseOutcome(
                case_id=case_id,
                kind=kind,
                victim_id=victim.id,
                victim_type=victim_type,
                attempted_cents=amount,
                decision=assessment.decision.value if assessment else None,
                points=assessment.points if assessment else None,
                reasons=[reason.rule for reason in assessment.reasons] if assessment else [],
                vop=assessment.vop.outcome.value if assessment else None,
                warned_and_ignored=ignored,
                executed=uetr is not None,
                receiver_screened=self.incoming is not None and assessment is not None,
                held_back=uetr is None and self.last_reason == HELD_BACK,
                uetr=uetr,
            )
            self.cases.append(case)
            if uetr is None:
                return
            delay = self.cfg.report_delays[
                int(_draw(self.cfg.seed, case_id, "report") * len(self.cfg.report_delays))
            ]
            case.report_delay = delay
            self.at(self.now + delay, self.report(case))
            self.schedule_mule(case, mule, amount, level=1)

        return run

    def schedule_mule(self, case: CaseOutcome, mule: _Party, received: int, level: int) -> None:
        cfg = self.cfg
        low, high = cfg.mule_forward_minutes
        wait = timedelta(
            minutes=low + int(_draw(cfg.seed, case.case_id, level, "wait") * (high - low))
        )
        keep = 0.9 + 0.1 * _draw(cfg.seed, case.case_id, level, "share")
        amount = max(1, int(received * keep))
        case.mule_levels = max(case.mule_levels, level)
        if level == 1 and _draw(cfg.seed, case.case_id, "second level") < cfg.second_mule_level:

            def forward() -> None:
                second = self.new_mule(case.case_id, 2)
                _, uetr, _ = self.instant_payment(
                    mule, second, second.name, amount, Actor.MULE, (case.case_id, "forward")
                )
                if uetr is not None:
                    self.schedule_mule(case, second, amount, level=2)
                else:
                    self.at(self.now + timedelta(minutes=5), self.cash_out(mule, amount))

            self.at(self.now + wait, forward)
        else:
            low, high = cfg.cash_out_minutes
            later = timedelta(
                minutes=low + int(_draw(cfg.seed, case.case_id, level, "cash") * (high - low))
            )
            self.at(self.now + later, self.cash_out(mule, amount))

    def cash_out(self, mule: _Party, cents: int) -> Callable[[], None]:
        def run() -> None:
            amount = min(cents, self.ledger.available_balance(mule.id))
            if amount <= 0:
                return
            try:
                self.ledger.transfer(
                    idempotency_key=self.key("cash"),
                    debtor_id=mule.id,
                    creditor_id=self.cash,
                    amount_cents=amount,
                    booked_at=self.now,
                )
            except InsufficientFunds:
                pass

        return run

    def report(self, case: CaseOutcome) -> Callable[[], None]:
        def run() -> None:
            upper = trace_payment(self.ledger, case.uetr, attribution=Attribution.OWN_FUNDS_FIRST)
            lower = trace_payment(
                self.ledger, case.uetr, attribution=Attribution.TRACED_FUNDS_FIRST
            )
            case.remaining_upper_cents = _on_customer_accounts(upper)
            case.remaining_lower_cents = _on_customer_accounts(lower)
            freeze = freeze_traced_funds(
                self.ledger,
                case.uetr,
                attribution=Attribution.TRACED_FUNDS_FIRST,
                reason=f"scam reported by the victim (case {case.case_id})",
                placed_by="victim's bank",
            )
            case.recovered_cents = freeze.trace.secured_cents + freeze.secured_cents
            after = trace_payment(
                self.ledger, case.uetr, attribution=Attribution.TRACED_FUNDS_FIRST
            )
            case.recovered_first_hop_cents = after.root.secured_cents

        return run

    # -- run ---------------------------------------------------------------------------------

    def run(self) -> None:
        self.build_world()
        self.build_scams()
        while self.queue:
            moment, _, action = heapq.heappop(self.queue)
            self.now = moment
            action()


def _on_customer_accounts(result) -> int:
    return sum(node.remaining_cents for node in result.nodes() if node.account_kind != SYSTEM)


def simulate(
    policy: Policy,
    config: ScenarioConfig | None = None,
    risk_config: RiskConfig | None = None,
    db_url: str | None = None,
) -> SimulationResult:
    """Run one policy on a fresh database. The same seed gives the same world for every policy."""
    config = config or ScenarioConfig()
    risk_config = risk_config or RiskConfig()
    with tempfile.TemporaryDirectory() as folder:
        url = db_url or f"sqlite:///{Path(folder) / 'simulation.db'}"
        run = _Run(config, policy, risk_config, url)
        try:
            run.run()
            ok = run.ledger.verify_invariants().ok
        finally:
            run.engine.dispose()
    return SimulationResult(
        policy=policy,
        config=config,
        risk_config=risk_config,
        cases=sorted(run.cases, key=lambda case: case.case_id),
        legit=run.legit,
        assessment_ms=run.latency,
        receiver_freezes=run.receiver_freezes,
        invariants_ok=ok,
    )
