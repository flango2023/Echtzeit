import pytest

from echtzeit.simulation import Policy, ScenarioConfig, percentile, simulate

SMALL = ScenarioConfig(
    seed=11,
    warmup_days=10,
    days=10,
    consumers=15,
    businesses=3,
    merchants=4,
    landlords=2,
    safe_account_cases=4,
    invoice_cases=4,
)


@pytest.fixture(scope="module")
def results():
    return {policy: simulate(policy, SMALL) for policy in Policy}


def summary(result):
    return [
        (c.case_id, c.kind, c.attempted_cents, c.decision, c.executed, c.recovered_cents)
        for c in result.cases
    ]


def test_every_run_keeps_the_ledger_consistent(results):
    for result in results.values():
        assert result.invariants_ok


def test_the_same_seed_gives_the_same_outcome(results):
    again = simulate(Policy.SENDER_AND_RECEIVER, SMALL)
    assert summary(again) == summary(results[Policy.SENDER_AND_RECEIVER])
    assert again.legit == results[Policy.SENDER_AND_RECEIVER].legit


def test_every_policy_faces_the_same_cases(results):
    shapes = {policy: [(c.case_id, c.kind) for c in r.cases] for policy, r in results.items()}
    assert len(set(map(tuple, shapes.values()))) == 1
    assert len(shapes[Policy.NONE]) == SMALL.safe_account_cases + SMALL.invoice_cases
    # Invoice amounts are fixed in the schedule, so they must be identical across policies.
    invoices = {
        policy: [c.attempted_cents for c in r.cases if c.kind == "INVOICE_IBAN_CHANGE"]
        for policy, r in results.items()
    }
    assert len(set(map(tuple, invoices.values()))) == 1


def test_without_scoring_every_scam_is_executed_and_nothing_is_assessed(results):
    none = results[Policy.NONE]
    assert all(c.executed and c.decision is None for c in none.cases)
    assert none.assessment_ms == []
    assert none.legit.warned == none.legit.blocked == 0
    assert none.receiver_freezes == 0


def test_case_accounting_adds_up(results):
    for result in results.values():
        for case in result.cases:
            assert 0 <= case.recovered_cents <= case.attempted_cents
            assert case.remaining_lower_cents <= case.remaining_upper_cents
            if case.executed:
                assert case.lost_cents + case.recovered_cents == case.attempted_cents
                assert case.report_delay is not None
            else:
                assert case.prevented_cents == case.attempted_cents
                assert case.lost_cents == 0 and case.recovered_cents == 0


def test_only_the_receiver_policy_freezes_on_the_receiving_side(results):
    assert results[Policy.SENDER_ONLY].receiver_freezes == 0
    assert results[Policy.NONE].receiver_freezes == 0


def test_scored_policies_record_latency_and_legit_statistics(results):
    for policy in (Policy.SENDER_ONLY, Policy.SENDER_AND_RECEIVER):
        result = results[policy]
        assert result.assessment_ms and all(ms > 0 for ms in result.assessment_ms)
        legit = result.legit
        assert legit.payments == legit.allowed + legit.warned + legit.blocked
        assert legit.warned_and_went_ahead <= legit.warned
        assert sum(legit.vop.values()) == legit.payments


@pytest.mark.parametrize(
    ("values", "percent", "expected"),
    [
        ([], 50, None),
        ([5.0], 99, 5.0),
        ([1, 2, 3, 4], 50, 2),
        ([1, 2, 3, 4], 75, 3),
        ([1, 2, 3, 4], 100, 4),
    ],
)
def test_nearest_rank_percentile(values, percent, expected):
    assert percentile(values, percent) == expected


def test_the_report_prints_the_numbers_of_the_run(results):
    from echtzeit.scenario_report import euro, render

    ordered = [results[policy] for policy in Policy]
    text = render(ordered, spread={1: ordered})

    lost = " | ".join(euro(r.total("lost_cents")) for r in ordered)
    assert f"| Lost | {lost} |" in text
    assert "## Spread over seeds 1–1" in text
    assert "Ledger invariants held in every run: yes." in text
    for case in ordered[0].cases:
        assert f"| {case.case_id} |" in text
    assert "evidence about real fraud" in text


def test_the_receiving_side_freeze_changes_the_outcome(results):
    """Sending-side decisions are identical, so any difference comes from the receiving freeze."""
    sender = results[Policy.SENDER_ONLY]
    both = results[Policy.SENDER_AND_RECEIVER]
    assert [c.decision for c in sender.cases] == [c.decision for c in both.cases]
    assert sender.total("prevented_cents") == both.total("prevented_cents")
    assert both.receiver_freezes > 0
    assert both.total("recovered_cents") > sender.total("recovered_cents")
    assert both.total("lost_cents") < sender.total("lost_cents")


def test_who_pays_shares_exactly_the_simulated_losses(results):
    from echtzeit.liability import REGIMES, Assumptions, totals
    from echtzeit.scenario_report import render

    for result in results.values():
        lost = result.total("lost_cents")
        for regime in REGIMES:
            t = totals(result.cases, regime, Assumptions())
            assert t.loss_cents == lost
            assert t.victim_cents + t.sending_psp_cents + t.receiving_psp_cents == lost
    text = render([results[policy] for policy in Policy])
    assert "## Who pays" in text and "| NONE | UK_PSR |" in text


def test_victim_types_follow_the_scenario(results):
    for case in results[Policy.NONE].cases:
        if case.kind == "SAFE_ACCOUNT":
            assert case.victim_type == "CONSUMER"
        else:
            assert case.victim_type in {"MICROENTERPRISE", "OTHER_BUSINESS"}
