from decimal import Decimal

import pytest

from echtzeit import calibration
from echtzeit.liability import UK_PSR, Assumptions
from echtzeit.simulation import CaseOutcome, Policy, ScenarioConfig, simulate


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
    }
    fields.update(overrides)
    return CaseOutcome(**fields)


def test_the_real_fraud_rate_means_about_fifty_thousand_normal_payments_per_scam():
    assert calibration.CT_FRAUD_RATE_BY_NUMBER.value == Decimal("0.00002")
    assert calibration.normal_payments_per_scam() == Decimal(49999)
    assert calibration.flagged_per_scam_at_real_rate(0.05) == Decimal("2499.95")
    assert calibration.flagged_per_scam_at_real_rate(0.0) == 0


@pytest.mark.parametrize("rate", [Decimal(0), Decimal(1), Decimal("-0.1")])
def test_invalid_rates_are_refused(rate):
    with pytest.raises(ValueError):
        calibration.normal_payments_per_scam(rate)
    with pytest.raises(ValueError):
        calibration.flagged_per_scam_at_real_rate(0.5, rate)


def test_flag_shares_outside_zero_to_one_are_refused():
    with pytest.raises(ValueError):
        calibration.flagged_per_scam_at_real_rate(1.5)


def test_every_reference_figure_names_a_primary_source_and_a_place_in_it():
    assert calibration.FIGURES
    for figure in calibration.FIGURES:
        assert figure.source.primary and figure.source.url.startswith("https://")
        assert figure.where


def test_uk_outcomes_count_only_claims_the_rules_cover():
    excess = UK_PSR.max_excess_cents
    cases = [
        case(case_id="A"),
        case(case_id="B", kind="INVOICE_IBAN_CHANGE", victim_type="OTHER_BUSINESS"),
        case(case_id="C", executed=False),
    ]
    result = calibration.uk_outcomes(cases, Assumptions())
    assert result.cases_in_scope == 1
    assert result.simulated_reimbursed_share == pytest.approx((500_000 - excess) / 500_000)
    assert result.simulated_rejected_for_care_share == 0
    assert calibration.uk_outcomes([cases[1]], Assumptions()).cases_in_scope == 0


def test_uk_outcomes_count_care_rejections_under_the_sensitivity_switch():
    warned = case(case_id="W", warned_and_ignored=True)
    strict = Assumptions(ignored_warning_is_gross_negligence=True)
    result = calibration.uk_outcomes([warned, case(case_id="A")], strict)
    assert result.cases_in_scope == 2
    assert result.simulated_rejected_for_care_share == 0.5


SMALL = ScenarioConfig(
    seed=5,
    warmup_days=10,
    days=10,
    consumers=15,
    businesses=3,
    merchants=4,
    landlords=2,
    safe_account_cases=4,
    invoice_cases=4,
    new_account_gifts=2,
)


def test_calibrated_amounts_stay_within_their_bounds():
    mean = 215_500
    result = simulate(
        Policy.NONE, ScenarioConfig(**{**SMALL.__dict__, "scam_amount_mean_cents": mean})
    )
    for c in result.cases:
        assert 1 <= c.attempted_cents <= mean * 3 // 2
        if c.kind == "INVOICE_IBAN_CHANGE":
            assert c.attempted_cents >= mean // 2
    assert result.invariants_ok


def test_uncalibrated_runs_keep_their_recorded_amounts():
    """Golden values: without a calibrated mean, scam amounts stay as they were recorded.

    If a deliberate model change moves them, update the numbers and regenerate the results.
    """
    assert SMALL.scam_amount_mean_cents is None
    plain = simulate(Policy.NONE, SMALL)
    amounts = [c.attempted_cents for c in plain.cases]
    assert (len(amounts), sum(amounts), plain.legit.payments) == (8, 3_590_066, 101)


def test_the_report_shows_the_real_rate_the_uk_check_and_the_sources():
    from echtzeit.scenario_report import flagged_share, render

    results = [simulate(policy, SMALL) for policy in Policy]
    calibrated = {
        1: [
            simulate(p, ScenarioConfig(**{**SMALL.__dict__, "scam_amount_mean_cents": 215_500}))
            for p in Policy
        ]
    }
    text = render(results, None, calibrated)
    assert "## At the real fraud rate" in text and "about one in 50,000" in text
    assert "0.002% of all credit transfers by number" in text
    for result in results:
        expected = calibration.flagged_per_scam_at_real_rate(flagged_share(result))
        assert f"| {result.policy.value} | {flagged_share(result):.2%} | {expected:,.0f} |" in text
    assert "## Sensitivity: scam amounts around EUR 2,155" in text
    assert "## UK regime compared with the PSR dashboard" in text and "88%" in text
    assert "## Reference figures" in text and "| 0.002% |" in text and "| 2,155 EUR |" in text
