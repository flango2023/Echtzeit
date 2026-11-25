"""Who pays for a scam loss under different reimbursement regimes.

Each regime is data: who is covered, which scams are covered, a cap, an excess and how the
cost is split between the sending and the receiving PSP. Every value carries the source it
was taken from. Applying a regime to a case gives an allocation of the loss between the
victim, the sending PSP and the receiving PSP, after money recovered by holds.

Regimes in version 1:

* UK_PSR: the UK Payment Systems Regulator's mandatory reimbursement for APP scams over
  Faster Payments and CHAPS, in force since 7 October 2024. Applied here *as if* the
  simulated euro payments were UK payments, with the sterling cap and excess converted at a
  fixed ECB reference rate. That is a what-if, not a statement about any real case.
* EU_PSR_PROVISIONAL: the consumer refund for fraud where the fraudster impersonates the
  consumer's PSP, from the provisional agreement on the EU Payment Services Regulation
  (27 November 2025). Not adopted and not in force when checked; the final text may differ.
* NO_REFUND: no reimbursement rule. The victim bears the loss. This stands in for markets
  without a refund rule.

Not modelled: claim deadlines and reimbursement time limits (the simulated victims report
within days, well inside them), vulnerability assessments, first-party fraud, the EU rules
on liability of online platforms towards PSPs, and liability for failing to offer
Verification of Payee (the simulation always runs it).
"""

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum

from echtzeit.simulation import CaseOutcome


class VictimType(Enum):
    CONSUMER = "CONSUMER"
    MICROENTERPRISE = "MICROENTERPRISE"
    OTHER_BUSINESS = "OTHER_BUSINESS"


class Status(Enum):
    IN_FORCE = "in force"
    PROVISIONAL = "provisional, not in force"
    BASELINE = "baseline"


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    primary: bool  # published by the regulator or legislator itself
    supports: str  # what the source was used for


# ECB euro foreign exchange reference rate, GBP per EUR, 24 November 2026.
ECB_GBP_PER_EUR = Decimal("0.83700")
ECB_RATE_DATE = "2026-11-24"
ECB_RATE_SOURCE = Source(
    "ECB euro foreign exchange reference rates, series EXR.D.GBP.EUR.SP00.A",
    "https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A",
    True,
    f"GBP per EUR on {ECB_RATE_DATE}, used to convert the UK cap and excess",
)


def gbp_pence_to_eur_cents(pence: int) -> int:
    """Convert at the fixed ECB rate, rounded to the nearest cent (half up)."""
    return int((Decimal(pence) / ECB_GBP_PER_EUR).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Regime:
    code: str
    name: str
    status: Status
    covered_victims: frozenset[VictimType]
    covered_kinds: frozenset[str] | None  # None: every APP scam kind
    cap_cents: int | None  # per claim, in euro cents; None: no cap
    max_excess_cents: int  # the most a sending PSP may deduct from the victim's refund
    receiving_share: Decimal  # share of the refund paid by the receiving PSP
    # When the sending PSP does not deduct the excess, the receiving PSP may lower its own
    # contribution by this amount (UK: 50% of the maximum excess).
    receiving_deduction_without_excess_cents: int
    sources: tuple[Source, ...]
    notes: tuple[str, ...] = ()


UK_PSR = Regime(
    code="UK_PSR",
    name="UK mandatory APP scam reimbursement (PSR)",
    status=Status.IN_FORCE,
    covered_victims=frozenset(
        {VictimType.CONSUMER, VictimType.MICROENTERPRISE}
    ),  # also charities, not simulated
    covered_kinds=None,
    cap_cents=gbp_pence_to_eur_cents(8_500_000),  # £85,000
    max_excess_cents=gbp_pence_to_eur_cents(10_000),  # £100
    receiving_share=Decimal("0.5"),
    receiving_deduction_without_excess_cents=gbp_pence_to_eur_cents(5_000),  # £50
    sources=(
        Source(
            "PSR, APP fraud reimbursement protections (consumer page)",
            "https://www.psr.org.uk/information-for-consumers/app-fraud-reimbursement-protections/",
            True,
            "scope (individuals, microenterprises, charities), start 7 October 2024, cap "
            "£85,000, optional excess up to £100, gross-negligence and complicity exceptions",
        ),
        Source(
            "PSR, PS24/7 Confirming the maximum level of reimbursement",
            "https://www.psr.org.uk/publications/policy-statements/ps247-faster-payments-app-scams-reimbursement-requirement-confirming-the-maximum-level-of-reimbursement/",
            True,
            "cap of £85,000 per claim from 7 October 2024",
        ),
        Source(
            "PSR, APP scams (overview)",
            "https://www.psr.org.uk/our-work/app-scams/",
            True,
            "cost split 50:50 between sending and receiving PSP",
        ),
        Source(
            "PSR, APP scams policy clarifications",
            "https://www.psr.org.uk/our-work/app-scams/policy-clarifications/",
            True,
            "receiving PSP may deduct 50% of the maximum excess; repatriated funds shared by "
            "the split; with mule accounts only the victim-to-first-mule payment is in scope "
            "and the PSP of the first mule is the receiving PSP",
        ),
        ECB_RATE_SOURCE,
    ),
    notes=(
        (
            "Applies to UK Faster Payments and CHAPS in sterling. Applied here as a what-if to "
            "simulated euro payments, with the cap and excess converted at the ECB rate."
        ),
    ),
)

EU_PSR_PROVISIONAL = Regime(
    code="EU_PSR_PROVISIONAL",
    name="EU PSR consumer refund for PSP impersonation (provisional)",
    status=Status.PROVISIONAL,
    covered_victims=frozenset({VictimType.CONSUMER}),
    covered_kinds=frozenset({"SAFE_ACCOUNT"}),
    cap_cents=None,
    max_excess_cents=0,
    receiving_share=Decimal(0),
    receiving_deduction_without_excess_cents=0,
    sources=(
        Source(
            "Council of the EU, press release of 27 November 2025 on the payment services deal",
            "https://www.consilium.europa.eu/en/press/press-releases/2025/11/27/payment-services-council-and-parliament-agree-to-step-up-the-fight-against-fraud-and-increase-transparency/",
            True,
            "provisional agreement; spoofing fraud where fraudsters impersonate the customer's "
            "PSP; PSPs liable when they do not use required preventive tools",
        ),
        Source(
            "European Parliament, press release 20251121IPR31540 (read through search-result "
            "excerpts; the page refused automated access)",
            "https://www.europarl.europa.eu/news/en/press-room/20251121IPR31540/payment-services-deal-more-protection-from-online-fraud-and-hidden-fees",
            True,
            "full refund when a scammer posing as a PSP employee tricks the customer, if the "
            "customer reports to the police and informs the PSP; receiving PSP must freeze "
            "transactions it finds suspicious",
        ),
        Source(
            "European Parliament, answer to written question E-000069/26",
            "https://www.europarl.europa.eu/doceo/document/E-10-2026-000069-ASW_EN.html",
            True,
            "the refund right is for consumers",
        ),
    ),
    notes=(
        (
            "Provisional text, not in force. The refund is paid by the consumer's own PSP; no "
            "split with the receiving PSP is modelled."
        ),
        (
            "A change of IBAN on an invoice is treated as an authorised payment, not as a "
            "transaction initiated or changed by the fraudster, so it is not covered."
        ),
    ),
)

NO_REFUND = Regime(
    code="NO_REFUND",
    name="No reimbursement rule",
    status=Status.BASELINE,
    covered_victims=frozenset(),
    covered_kinds=frozenset(),
    cap_cents=None,
    max_excess_cents=0,
    receiving_share=Decimal(0),
    receiving_deduction_without_excess_cents=0,
    sources=(),
    notes=("The victim bears every loss that is not recovered.",),
)

REGIMES = (UK_PSR, EU_PSR_PROVISIONAL, NO_REFUND)


@dataclass(frozen=True)
class Assumptions:
    """Facts the simulation does not know. Each one changes the outcome and is printed."""

    sending_psp_applies_excess: bool = True
    victims_report_to_police_and_psp: bool = True
    # Ignoring a bank warning is not gross negligence by itself under the UK rules, which set
    # a high bar. Switching this on is a sensitivity test, not a reading of the rules.
    ignored_warning_is_gross_negligence: bool = False


@dataclass(frozen=True)
class Allocation:
    case_id: str
    regime: str
    covered: bool
    reason: str
    loss_cents: int  # paid amount minus money recovered: what the parties share between them
    victim_cents: int
    sending_psp_cents: int
    receiving_psp_cents: int
    refunded_cents: int = 0  # paid out to the victim by the PSPs before recoveries
    excess_cents: int = 0
    above_cap_cents: int = 0


@dataclass(frozen=True)
class Totals:
    regime: str
    cases: int
    covered: int
    loss_cents: int
    victim_cents: int
    sending_psp_cents: int
    receiving_psp_cents: int
    allocations: list[Allocation] = field(default_factory=list)

    @property
    def psp_cents(self) -> int:
        return self.sending_psp_cents + self.receiving_psp_cents


def allocate(case: CaseOutcome, regime: Regime, assumptions: Assumptions) -> Allocation | None:
    """Share the loss of one paid scam. Returns None for a scam payment that was never made."""
    if not case.executed:
        return None
    paid = case.attempted_cents
    recovered = case.recovered_cents
    loss = paid - recovered
    victim_type = VictimType(case.victim_type)

    def uncovered(reason: str) -> Allocation:
        return Allocation(case.case_id, regime.code, False, reason, loss, loss, 0, 0)

    if victim_type not in regime.covered_victims:
        return uncovered(f"victim type {victim_type.value} not covered")
    if regime.covered_kinds is not None and case.kind not in regime.covered_kinds:
        return uncovered(f"scam kind {case.kind} not covered")
    if not assumptions.victims_report_to_police_and_psp:
        return uncovered("not reported to the police and the PSP")
    if assumptions.ignored_warning_is_gross_negligence and case.warned_and_ignored:
        return uncovered("ignored a warning, treated as gross negligence (sensitivity)")

    capped = paid if regime.cap_cents is None else min(paid, regime.cap_cents)
    above_cap = paid - capped
    excess = min(regime.max_excess_cents, capped) if assumptions.sending_psp_applies_excess else 0
    refunded = capped - excess

    # Rounded down: when the refund has an odd cent, the sending PSP, which pays the victim,
    # carries it. This rounding rule is this project's choice.
    receiving = int(
        (Decimal(refunded) * regime.receiving_share).to_integral_value(rounding=ROUND_DOWN)
    )
    if not assumptions.sending_psp_applies_excess and regime.receiving_share > 0:
        receiving = max(0, receiving - regime.receiving_deduction_without_excess_cents)
    sending = refunded - receiving

    # Recovered money first covers what the PSPs paid, shared by their split; PSPs may not keep
    # more than they paid, so any rest goes back to the victim.
    to_psps = min(recovered, refunded)
    receiving_back = (to_psps * receiving) // refunded if refunded else 0
    sending_back = to_psps - receiving_back
    victim_back = recovered - to_psps

    return Allocation(
        case_id=case.case_id,
        regime=regime.code,
        covered=True,
        reason="covered",
        loss_cents=loss,
        victim_cents=paid - refunded - victim_back,
        sending_psp_cents=sending - sending_back,
        receiving_psp_cents=receiving - receiving_back,
        refunded_cents=refunded,
        excess_cents=excess,
        above_cap_cents=above_cap,
    )


def totals(cases: list[CaseOutcome], regime: Regime, assumptions: Assumptions) -> Totals:
    allocations = [a for a in (allocate(c, regime, assumptions) for c in cases) if a is not None]
    return Totals(
        regime=regime.code,
        cases=len(allocations),
        covered=sum(a.covered for a in allocations),
        loss_cents=sum(a.loss_cents for a in allocations),
        victim_cents=sum(a.victim_cents for a in allocations),
        sending_psp_cents=sum(a.sending_psp_cents for a in allocations),
        receiving_psp_cents=sum(a.receiving_psp_cents for a in allocations),
        allocations=allocations,
    )
