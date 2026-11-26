import itertools
from dataclasses import replace

import pytest

from echtzeit.liability import (
    BR_MED,
    EU_PSR_PROVISIONAL,
    NO_REFUND,
    REGIMES,
    UK_PSR,
    Assumptions,
    allocate,
    gbp_pence_to_eur_cents,
    totals,
)
from echtzeit.simulation import CaseOutcome

DEFAULT = Assumptions()
EXCESS = UK_PSR.max_excess_cents  # £100 in euro cents


def case(**overrides) -> CaseOutcome:
    fields = {
        "case_id": "C1",
        "kind": "SAFE_ACCOUNT",
        "victim_id": "v",
        "victim_type": "CONSUMER",
        "attempted_cents": 500_000,
        "decision": None,
        "points": None,
        "reasons": [],
        "vop": None,
        "warned_and_ignored": False,
        "executed": True,
        "recovered_cents": 0,
    }
    fields.update(overrides)
    # Unless a test says otherwise, all recovered money was on the first receiving account.
    fields.setdefault("recovered_first_hop_cents", fields["recovered_cents"])
    return CaseOutcome(**fields)


def parts(allocation):
    return allocation.victim_cents, allocation.sending_psp_cents, allocation.receiving_psp_cents


def test_conversion_uses_the_fixed_ecb_rate():
    assert gbp_pence_to_eur_cents(8_500_000) == 10_155_317  # £85,000 / 0.837
    assert UK_PSR.cap_cents == 10_155_317
    assert EXCESS == 11_947


def test_a_scam_payment_that_was_never_made_has_nothing_to_share():
    for regime in REGIMES:
        assert allocate(case(executed=False), regime, DEFAULT) is None


def test_uk_consumer_is_refunded_minus_the_excess_and_banks_split_the_cost():
    a = allocate(case(), UK_PSR, DEFAULT)
    assert a.covered
    assert a.refunded_cents == 500_000 - EXCESS
    assert a.victim_cents == EXCESS
    # 488,053 cents cannot be halved; the sending bank, which pays the victim, carries the odd cent.
    assert a.receiving_psp_cents == (500_000 - EXCESS) // 2
    assert a.sending_psp_cents == a.receiving_psp_cents + 1
    assert sum(parts(a)) == 500_000


def test_uk_without_excess_the_receiving_bank_deducts_half_the_maximum_excess():
    a = allocate(case(), UK_PSR, Assumptions(sending_psp_applies_excess=False))
    deduction = UK_PSR.receiving_deduction_without_excess_cents
    assert a.victim_cents == 0
    assert a.receiving_psp_cents == 250_000 - deduction
    assert a.sending_psp_cents == 250_000 + deduction


def test_uk_cap_leaves_the_amount_above_it_with_the_victim():
    paid = UK_PSR.cap_cents + 1_000_000
    a = allocate(case(attempted_cents=paid), UK_PSR, DEFAULT)
    assert a.above_cap_cents == 1_000_000
    assert a.victim_cents == 1_000_000 + EXCESS
    assert a.sending_psp_cents + a.receiving_psp_cents == UK_PSR.cap_cents - EXCESS


def test_uk_recovered_money_goes_to_the_banks_first_by_their_split():
    a = allocate(case(recovered_cents=200_000), UK_PSR, DEFAULT)
    refunded = 500_000 - EXCESS
    assert a.victim_cents == EXCESS
    assert a.sending_psp_cents + a.receiving_psp_cents == refunded - 200_000
    assert abs(a.sending_psp_cents - a.receiving_psp_cents) <= 1


def test_uk_banks_never_keep_more_than_they_paid():
    a = allocate(case(recovered_cents=500_000), UK_PSR, DEFAULT)
    assert parts(a) == (0, 0, 0)
    b = allocate(case(recovered_cents=495_000), UK_PSR, DEFAULT)
    assert b.sending_psp_cents == b.receiving_psp_cents == 0
    assert b.victim_cents == 5_000  # the victim gets the rest of the recovery back


@pytest.mark.parametrize(
    ("victim_type", "covered"),
    [("CONSUMER", True), ("MICROENTERPRISE", True), ("OTHER_BUSINESS", False)],
)
def test_uk_scope_by_victim_type(victim_type, covered):
    a = allocate(case(kind="INVOICE_IBAN_CHANGE", victim_type=victim_type), UK_PSR, DEFAULT)
    assert a.covered is covered
    if not covered:
        assert parts(a) == (500_000, 0, 0)


MONITORED = Assumptions(receiving_banks_monitor_incoming=True)


def test_eu_without_monitoring_by_both_banks_the_payers_bank_carries_the_loss():
    """Art. 87(3): no scoring at all, or no screening of incoming payments by the payee's bank."""
    for victim_type, kind in (
        ("CONSUMER", "SAFE_ACCOUNT"),
        ("OTHER_BUSINESS", "INVOICE_IBAN_CHANGE"),
    ):
        unscored = case(victim_type=victim_type, kind=kind, recovered_cents=100_000)
        a = allocate(unscored, EU_PSR_PROVISIONAL, DEFAULT)
        assert a.covered and a.reason.startswith("art. 87(3)")
        assert parts(a) == (0, 400_000, 0)
        scored = case(
            victim_type=victim_type, kind=kind, decision="ALLOW", points=20, reasons=["NEW_PAYEE"]
        )
        assert allocate(scored, EU_PSR_PROVISIONAL, DEFAULT).reason.startswith("art. 87(3)")


def test_eu_warned_payment_with_real_suspicion_signals_moves_the_loss_to_the_bank():
    warned = case(
        kind="INVOICE_IBAN_CHANGE",
        victim_type="OTHER_BUSINESS",
        decision="WARN",
        points=75,
        reasons=["NEW_PAYEE", "VOP_NO_MATCH", "FRESH_RECEIVER"],
        warned_and_ignored=True,
    )
    a = allocate(warned, EU_PSR_PROVISIONAL, MONITORED)
    assert a.covered and a.reason.startswith("art. 68(2)")
    assert parts(a) == (0, 500_000, 0)


def test_eu_unusualness_and_payee_name_alone_are_not_justified_suspicion():
    warned = case(
        kind="INVOICE_IBAN_CHANGE",
        victim_type="OTHER_BUSINESS",
        decision="WARN",
        points=60,
        reasons=["NEW_PAYEE", "VOP_NO_MATCH"],
        warned_and_ignored=True,
    )
    a = allocate(warned, EU_PSR_PROVISIONAL, MONITORED)
    assert not a.covered
    assert a.victim_cents == a.loss_cents


def test_eu_impersonation_refund_and_its_conditions():
    scam = case(decision="ALLOW", points=20, reasons=["NEW_PAYEE"])
    a = allocate(scam, EU_PSR_PROVISIONAL, MONITORED)
    assert a.covered and a.reason.startswith("art. 60")
    assert parts(a) == (0, 500_000, 0)
    for changed in (
        Assumptions(receiving_banks_monitor_incoming=True, impersonation_via_bank_channels=False),
        Assumptions(receiving_banks_monitor_incoming=True, victims_report_to_police_and_psp=False),
    ):
        assert not allocate(scam, EU_PSR_PROVISIONAL, changed).covered
    business = case(
        decision="ALLOW", points=20, reasons=["NEW_PAYEE"], victim_type="MICROENTERPRISE"
    )
    assert not allocate(business, EU_PSR_PROVISIONAL, MONITORED).covered


def test_eu_gross_negligence_only_removes_the_impersonation_refund():
    ignored = case(
        decision="WARN", points=45, reasons=["NEW_PAYEE", "AMOUNT_SPIKE"], warned_and_ignored=True
    )
    strict = Assumptions(
        receiving_banks_monitor_incoming=True, ignored_warning_is_gross_negligence=True
    )
    assert not allocate(ignored, EU_PSR_PROVISIONAL, strict).covered
    # Under art. 68(2) and 87(3) only the payer's own fraud removes the protection.
    suspicious = replace(ignored, reasons=["NEW_PAYEE", "PASS_THROUGH"])
    assert allocate(suspicious, EU_PSR_PROVISIONAL, strict).covered
    assert allocate(
        ignored, EU_PSR_PROVISIONAL, Assumptions(ignored_warning_is_gross_negligence=True)
    ).covered


def test_no_refund_leaves_the_loss_with_the_victim():
    a = allocate(case(recovered_cents=50_000), NO_REFUND, DEFAULT)
    assert not a.covered
    assert parts(a) == (450_000, 0, 0)


def test_br_med_returns_traced_money_and_refunds_nothing():
    a = allocate(case(recovered_cents=300_000, recovered_first_hop_cents=100_000), BR_MED, DEFAULT)
    assert not a.covered
    assert parts(a) == (200_000, 0, 0)  # money recovered along the whole path counts


def test_each_regime_uses_its_own_recovery_reach():
    deep = case(recovered_cents=300_000, recovered_first_hop_cents=100_000)
    for regime in (UK_PSR, EU_PSR_PROVISIONAL, NO_REFUND):
        assert allocate(deep, regime, DEFAULT).loss_cents == 400_000  # first account only
    assert allocate(deep, BR_MED, DEFAULT).loss_cents == 200_000  # along the traced path


def test_sensitivity_switches_remove_the_uk_refund():
    warned = case(warned_and_ignored=True)
    assert allocate(warned, UK_PSR, DEFAULT).covered
    assert not allocate(
        warned, UK_PSR, Assumptions(ignored_warning_is_gross_negligence=True)
    ).covered
    assert not allocate(case(), UK_PSR, Assumptions(victims_report_to_police_and_psp=False)).covered


def test_every_allocation_shares_exactly_the_unrecovered_loss():
    """Victim plus both banks always equals paid minus recovered, whatever the inputs."""
    amounts = [1, 99, 11_947, 11_948, 500_000, UK_PSR.cap_cents, UK_PSR.cap_cents + 7]
    for paid, regime, excess, victim_type, kind in itertools.product(
        amounts,
        REGIMES,
        (True, False),
        ("CONSUMER", "MICROENTERPRISE", "OTHER_BUSINESS"),
        ("SAFE_ACCOUNT", "INVOICE_IBAN_CHANGE"),
    ):
        for recovered, (decision, screened) in itertools.product(
            {0, 1, paid // 3, paid - 1, paid},
            # Every EU branch: no score, art. 68(2) suspicion, art. 87(3) on or off.
            [(None, False), ("ALLOW", True), ("WARN", False), ("WARN", True), ("BLOCK", True)],
        ):
            c = case(
                attempted_cents=paid,
                recovered_cents=recovered,
                victim_type=victim_type,
                kind=kind,
                decision=decision,
                points=None if decision is None else 50,
                reasons=[] if decision is None else ["FRESH_RECEIVER"],
                warned_and_ignored=decision == "WARN",
                receiver_screened=screened,
            )
            a = allocate(c, regime, Assumptions(sending_psp_applies_excess=excess))
            assert sum(parts(a)) == paid - recovered == a.loss_cents
            assert min(parts(a)) >= 0


def test_totals_add_up_over_cases():
    cases = [
        case(case_id="A"),
        case(case_id="B", executed=False),
        case(case_id="C", kind="INVOICE_IBAN_CHANGE", victim_type="OTHER_BUSINESS"),
    ]
    t = totals(cases, UK_PSR, DEFAULT)
    assert t.cases == 2 and t.covered == 1
    assert t.victim_cents + t.psp_cents == t.loss_cents == 1_000_000
    assert [a.case_id for a in t.allocations] == ["A", "C"]


def test_every_regime_that_moves_money_cites_primary_sources():
    for regime in (UK_PSR, EU_PSR_PROVISIONAL, BR_MED):
        assert regime.sources
        assert any(source.primary for source in regime.sources)
        assert all(source.url.startswith("https://") for source in regime.sources)
    assert replace(NO_REFUND).sources == ()


def test_eu_a_case_screened_by_the_payees_bank_meets_art_87():
    screened = case(decision="ALLOW", points=20, reasons=["NEW_PAYEE"], receiver_screened=True)
    a = allocate(screened, EU_PSR_PROVISIONAL, DEFAULT)
    assert a.reason.startswith("art. 60")
    business = replace(screened, kind="INVOICE_IBAN_CHANGE", victim_type="OTHER_BUSINESS")
    assert not allocate(business, EU_PSR_PROVISIONAL, DEFAULT).covered
