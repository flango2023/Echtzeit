"""Real reference figures and what the simulation's results mean against them.

The simulation's world is synthetic, and so is every number it produces. This module holds the
few real, published figures that can anchor it. Each figure records its source and page, and
none is typed anywhere else. They are aggregates: they calibrate and sanity-check the
simulation, they do not replace it.

Three uses:

* Base rate. In the simulation about 3% of payments are scams; in reality fraudulent credit
  transfers are about 0.002% of all credit transfers by number. A warning rate on normal
  payments that looks modest in the simulation means many warnings per real scam.
  `flagged_per_scam_at_real_rate` turns the one into the other.
* Amounts. `ScenarioConfig.scam_amount_mean_cents` can be set to the EBA-ECB average value of a
  fraudulent credit transfer, as a sensitivity run.
* UK outcomes. The PSR publishes how much was reimbursed under its rules. The report compares
  the simulated UK regime with it; the comparison shows what the model leaves out, it is not
  used to tune anything.
"""

from dataclasses import dataclass
from decimal import Decimal

from echtzeit.liability import UK_PSR, Assumptions, Source, allocate
from echtzeit.simulation import CaseOutcome

EBA_ECB_2025 = Source(
    "EBA and ECB, 2025 Report on payment fraud (published December 2025, data for 2024)",
    "https://www.eba.europa.eu/sites/default/files/2025-12/1709846a-84d9-47cf-86a0-b155efb34d66/EBA%20and%20ECB%20Report%20on%20Payment%20Fraud.pdf",
    True,
    "EU/EEA credit transfers in 2024",
)
PSR_DASHBOARD_Q1_2026 = Source(
    "Payment Systems Regulator, APP scams reimbursement dashboard for Q1 2026",
    "https://www.psr.org.uk/information-for-consumers/app-scams-reimbursement-dashboard/",
    True,
    "UK outcomes from 7 October 2024 to 31 March 2026",
)


@dataclass(frozen=True)
class Figure:
    name: str
    value: Decimal
    unit: str
    source: Source
    where: str  # page or section in the source
    note: str = ""


# EBA-ECB 2025, page 15: about 1,166,000 fraudulent credit transfers in 2024, a fraud rate of
# 0.002% by number of transactions. (Page 6 gives 0.001% by value, EUR 2.5 billion.)
CT_FRAUD_RATE_BY_NUMBER = Figure(
    "Fraudulent credit transfers as a share of all credit transfers, by number",
    Decimal("0.00002"),
    "share",
    EBA_ECB_2025,
    "page 15",
    "all reported credit-transfer fraud, authorised and unauthorised, and all credit transfers",
)
CT_FRAUD_RATE_BY_VALUE = Figure(
    "Fraudulent credit transfers as a share of all credit transfers, by value",
    Decimal("0.00001"),
    "share",
    EBA_ECB_2025,
    "page 6",
)
CT_FRAUD_AVERAGE_EUR = Figure(
    "Average value of a fraudulent credit transfer",
    Decimal(2155),
    "EUR",
    EBA_ECB_2025,
    "page 15",
    "all credit-transfer fraud types together",
)
CT_LOSS_SHARE_BORNE_BY_USERS = Figure(
    "Share of credit-transfer fraud losses borne by payment service users",
    Decimal("0.85"),
    "share",
    EBA_ECB_2025,
    "page 7 ('around 85%')",
    "under the rules in force in 2024, before the Payment Services Regulation",
)
UK_REIMBURSED_SHARE = Figure(
    "Share of money lost to APP scams that was reimbursed",
    Decimal("0.88"),
    "share",
    PSR_DASHBOARD_Q1_2026,
    "18-month summary (GBP 316m)",
)
UK_REJECTED_FOR_CARE_SHARE = Figure(
    "Share of claims rejected because the customer did not take enough care",
    Decimal("0.03"),
    "share",
    PSR_DASHBOARD_Q1_2026,
    "18-month summary",
)

FIGURES = (
    CT_FRAUD_RATE_BY_NUMBER,
    CT_FRAUD_RATE_BY_VALUE,
    CT_FRAUD_AVERAGE_EUR,
    CT_LOSS_SHARE_BORNE_BY_USERS,
    UK_REIMBURSED_SHARE,
    UK_REJECTED_FOR_CARE_SHARE,
)


def normal_payments_per_scam(fraud_rate: Decimal = CT_FRAUD_RATE_BY_NUMBER.value) -> Decimal:
    """How many normal payments there are for each fraudulent one at a given fraud rate."""
    if not 0 < fraud_rate < 1:
        raise ValueError("fraud_rate must be between 0 and 1")
    return (1 - fraud_rate) / fraud_rate


def flagged_per_scam_at_real_rate(
    flagged_share: float, fraud_rate: Decimal = CT_FRAUD_RATE_BY_NUMBER.value
) -> Decimal:
    """Normal payments flagged for each scam payment, if the simulated flag rate met reality.

    This assumes the flag rate measured on simulated normal payments carries over to real ones,
    which the simulation cannot show. It is an order of magnitude, not a forecast.
    """
    if not 0 <= flagged_share <= 1:
        raise ValueError("flagged_share must be between 0 and 1")
    return Decimal(str(flagged_share)) * normal_payments_per_scam(fraud_rate)


@dataclass(frozen=True)
class UkComparison:
    simulated_reimbursed_share: float | None  # refunded / paid, over cases the UK rules cover
    simulated_rejected_for_care_share: float | None  # covered-type claims refused for care
    cases_in_scope: int


def uk_outcomes(cases: list[CaseOutcome], assumptions: Assumptions) -> UkComparison:
    """The simulated UK regime in the terms the PSR dashboard uses.

    In scope means the victim type is covered (consumer or microenterprise). The reimbursed
    share is the money refunded over the money paid in those claims.
    """
    in_scope = []
    for case in cases:
        allocation = allocate(case, UK_PSR, assumptions)
        if allocation is None:
            continue
        if allocation.covered or allocation.reason.startswith("ignored a warning"):
            in_scope.append((case, allocation))
    if not in_scope:
        return UkComparison(None, None, 0)
    paid = sum(case.attempted_cents for case, _ in in_scope)
    refunded = sum(a.refunded_cents for _, a in in_scope)
    refused = sum(1 for _, a in in_scope if not a.covered)
    return UkComparison(refunded / paid, refused / len(in_scope), len(in_scope))
