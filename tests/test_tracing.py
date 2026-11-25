import itertools
import random

import pytest

from echtzeit.errors import InsufficientFunds, InvalidRequest, TransferNotFound
from echtzeit.iso20022.identifiers import new_uetr
from echtzeit.tracing import Attribution, freeze_traced_funds, trace_payment

OWN = Attribution.OWN_FUNDS_FIRST
TRACED = Attribution.TRACED_FUNDS_FIRST
BOTH = [OWN, TRACED]


class Net:
    """A small helper that creates accounts and numbered payments."""

    def __init__(self, ledger, fund):
        self.ledger = ledger
        self._fund = fund
        self._keys = itertools.count(1)

    def account(self, name: str, funds: int = 0) -> str:
        account_id = self.ledger.create_account(name)
        if funds:
            self._fund(account_id, funds, key=f"fund-{name}")
        return account_id

    def pay(self, debtor: str, creditor: str, cents: int) -> str:
        """Make a payment and return its UETR."""
        uetr = new_uetr()
        self.ledger.transfer(
            idempotency_key=f"p-{next(self._keys)}",
            debtor_id=debtor,
            creditor_id=creditor,
            amount_cents=cents,
            uetr=uetr,
        )
        return uetr


@pytest.fixture
def net(ledger, fund):
    return Net(ledger, fund)


def assert_conserved(result):
    assert result.traced_cents == result.remaining_cents + result.unfollowed_cents


def by_account(result):
    return {node.account_id: node for node in result.nodes()}


def test_pass_through_chain_ends_at_the_last_account(ledger, net):
    victim, m1, m2, m3 = (net.account(n, 1_000 if n == "v" else 0) for n in ("v", "m1", "m2", "m3"))
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 1_000)
    net.pay(m2, m3, 1_000)

    for attribution in BOTH:
        result = trace_payment(ledger, uetr, attribution=attribution)
        nodes = by_account(result)
        assert [n.depth for n in result.nodes()] == [0, 1, 2]
        assert nodes[m1].remaining_cents == 0 and nodes[m2].remaining_cents == 0
        assert nodes[m3].remaining_cents == 1_000
        assert result.remaining_cents == 1_000
        assert_conserved(result)


def test_the_two_rules_give_an_upper_and_a_lower_bound_on_mixed_money(ledger, net):
    victim = net.account("v", 1_000)
    m1 = net.account("m1", 500)  # money of its own, there before the scam payment
    m2 = net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 700)

    upper = trace_payment(ledger, uetr, attribution=OWN)
    lower = trace_payment(ledger, uetr, attribution=TRACED)

    assert upper.root.remaining_cents == 800 and by_account(upper)[m2].traced_in_cents == 200
    assert lower.root.remaining_cents == 300 and by_account(lower)[m2].traced_in_cents == 700
    assert upper.root.remaining_cents >= lower.root.remaining_cents
    assert_conserved(upper) and assert_conserved(lower)


def test_a_credit_that_is_only_partly_traced_leaves_the_rest_as_other_money(ledger, net):
    """Regression: the untraced part of a credit must count as other money on the new account."""
    victim = net.account("v", 1_000)
    m1 = net.account("m1", 500)
    m2, m3 = net.account("m2"), net.account("m3")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 700)  # carries only 200 traced money under OWN_FUNDS_FIRST
    net.pay(m2, m3, 700)

    upper = trace_payment(ledger, uetr, attribution=OWN)
    lower = trace_payment(ledger, uetr, attribution=TRACED)

    assert by_account(upper)[m3].traced_in_cents == 200  # m2 spent its 500 of other money first
    assert by_account(lower)[m3].traced_in_cents == 700
    assert_conserved(upper) and assert_conserved(lower)


def test_two_arrivals_on_one_account_are_not_counted_twice(ledger, net):
    """Regression: traced money reaching an account by two paths shares one pool there."""
    victim = net.account("v", 1_000)
    m1, m2, m3, m4 = (net.account(n) for n in ("m1", "m2", "m3", "m4"))
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 300)
    net.pay(m1, m3, 700)
    net.pay(m3, m2, 400)
    net.pay(m2, m4, 500)

    for attribution in BOTH:
        result = trace_payment(ledger, uetr, attribution=attribution)
        nodes = by_account(result)
        assert len(nodes) == len(list(result.nodes()))  # one entry per account
        assert nodes[m2].traced_in_cents == 700
        assert nodes[m2].remaining_cents == 200 == ledger.balance(m2)
        assert nodes[m2].depth == 1
        assert nodes[m4].traced_in_cents == 500
        assert result.remaining_cents == 1_000
        assert_conserved(result)


def test_money_coming_back_to_an_account_it_left_is_tracked_once(ledger, net):
    victim = net.account("v", 1_000)
    m1, m2 = net.account("m1"), net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 1_000)
    net.pay(m2, m1, 600)

    for attribution in BOTH:
        result = trace_payment(ledger, uetr, attribution=attribution)
        nodes = by_account(result)
        assert nodes[m1].remaining_cents == 600 and nodes[m2].remaining_cents == 400
        assert nodes[m1].traced_in_cents == 1_600
        assert result.remaining_cents == 1_000
        assert_conserved(result)


def test_money_paid_back_to_the_victim_shows_up_on_the_victim_account(ledger, net):
    victim = net.account("v", 1_000)
    m1 = net.account("m1")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, victim, 250)

    result = trace_payment(ledger, uetr, attribution=TRACED)

    assert by_account(result)[victim].remaining_cents == 250
    assert result.root.remaining_cents == 750
    assert_conserved(result)


def test_money_arriving_later_counts_as_other_money(ledger, net, fund):
    victim = net.account("v", 1_000)
    m1 = net.account("m1")
    m2 = net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    fund(m1, 500, key="later")
    net.pay(m1, m2, 1_200)

    upper = trace_payment(ledger, uetr, attribution=OWN)
    lower = trace_payment(ledger, uetr, attribution=TRACED)

    assert upper.root.remaining_cents == 300  # 500 of the 1,200 was other money
    assert lower.root.remaining_cents == 0  # traced money went first
    assert by_account(lower)[m2].traced_in_cents == 1_000


def test_a_payment_split_over_several_accounts_gets_several_children(ledger, net):
    victim = net.account("v", 1_000)
    m1, m2, m3 = net.account("m1"), net.account("m2"), net.account("m3")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 300)
    net.pay(m1, m3, 400)

    result = trace_payment(ledger, uetr, attribution=TRACED)

    assert result.root.remaining_cents == 300
    assert sorted(hop.traced_cents for hop in result.hops_from(m1)) == [300, 400]
    assert result.remaining_cents == 1_000
    assert_conserved(result)


def test_depth_limit_stops_the_trace_and_reports_what_was_not_followed(ledger, net):
    victim = net.account("v", 1_000)
    chain = [net.account(f"m{i}") for i in range(1, 5)]
    uetr = net.pay(victim, chain[0], 1_000)
    for source, target in itertools.pairwise(chain):
        net.pay(source, target, 1_000)

    result = trace_payment(ledger, uetr, attribution=TRACED, max_depth=2)

    assert [n.account_id for n in result.nodes()] == chain[:3]
    assert result.remaining_cents == 0
    assert result.unfollowed_cents == 1_000
    assert_conserved(result)

    shallow = trace_payment(ledger, uetr, attribution=TRACED, max_depth=0)
    assert [n.account_id for n in shallow.nodes()] == [chain[0]]
    assert shallow.unfollowed_cents == 1_000
    assert_conserved(shallow)


def test_trace_input_is_validated(ledger, net):
    victim, m1 = net.account("v", 100), net.account("m1")
    uetr = net.pay(victim, m1, 100)
    with pytest.raises(TransferNotFound):
        trace_payment(ledger, new_uetr(), attribution=TRACED)
    with pytest.raises(InvalidRequest):
        trace_payment(ledger, uetr, attribution="TRACED_FUNDS_FIRST")
    with pytest.raises(InvalidRequest):
        trace_payment(ledger, uetr, attribution=TRACED, max_depth=-1)
    with pytest.raises(TypeError):
        trace_payment(ledger, uetr)  # the attribution rule has no default


def test_tracing_changes_nothing(ledger, net):
    victim, m1 = net.account("v", 100), net.account("m1")
    uetr = net.pay(victim, m1, 100)
    before = (ledger.balance(victim), ledger.balance(m1), ledger.active_holds(m1))

    trace_payment(ledger, uetr, attribution=OWN)

    assert (ledger.balance(victim), ledger.balance(m1), ledger.active_holds(m1)) == before


# -- freezing -------------------------------------------------------------------------------


def test_freeze_holds_the_money_on_every_account_it_is_found(ledger, net):
    victim = net.account("v", 1_000)
    m1, m2 = net.account("m1"), net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 600)

    freeze = freeze_traced_funds(
        ledger, uetr, attribution=TRACED, reason="reported scam", placed_by="test"
    )

    assert freeze.requested_cents == 1_000 and freeze.secured_cents == 1_000
    assert {h.account_id: h.amount_cents for h in freeze.holds} == {m1: 400, m2: 600}
    assert all(h.uetr == uetr for h in freeze.holds)
    assert ledger.available_balance(m1) == 0 and ledger.available_balance(m2) == 0
    with pytest.raises(InsufficientFunds):
        net.pay(m1, victim, 1)
    after = trace_payment(ledger, uetr, attribution=TRACED)
    assert after.secured_cents == 1_000
    assert ledger.verify_invariants().ok


def test_freezing_twice_does_not_hold_the_same_money_again(ledger, net):
    victim, m1 = net.account("v", 1_000), net.account("m1")
    uetr = net.pay(victim, m1, 1_000)
    kwargs = {"attribution": TRACED, "reason": "scam", "placed_by": "test"}

    first = freeze_traced_funds(ledger, uetr, **kwargs)
    second = freeze_traced_funds(ledger, uetr, **kwargs)

    assert first.secured_cents == 1_000
    assert second.holds == [] and second.requested_cents == 0
    assert len(ledger.active_holds(m1)) == 1


def test_a_freeze_can_secure_less_than_it_found(ledger, net):
    """Traced money is on the account, but part of the balance is already held for another reason."""
    victim = net.account("v", 1_000)
    m1 = net.account("m1", 300)
    uetr = net.pay(victim, m1, 1_000)
    ledger.place_hold(m1, 1_100, reason="other case", placed_by="test")  # 200 spendable left

    freeze = freeze_traced_funds(ledger, uetr, attribution=OWN, reason="scam", placed_by="test")

    assert freeze.requested_cents == 1_000
    assert freeze.secured_cents == 200


def test_the_freeze_is_atomic_when_a_later_step_fails(ledger, net, monkeypatch):
    victim = net.account("v", 1_000)
    m1, m2 = net.account("m1"), net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 400)

    from echtzeit import tracing

    real = tracing.holds.freeze_available
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("simulated failure on the second hold")
        return real(*args, **kwargs)

    monkeypatch.setattr(tracing.holds, "freeze_available", flaky)
    with pytest.raises(RuntimeError):
        freeze_traced_funds(ledger, uetr, attribution=TRACED, reason="scam", placed_by="test")
    monkeypatch.undo()

    assert ledger.active_holds(m1) == [] and ledger.active_holds(m2) == []
    assert ledger.verify_invariants().ok


def test_held_traced_money_stays_traced_when_other_money_moves_in_and_out(ledger, net, fund):
    """Without pins, 'traced funds first' would treat the later payment as spending the held money."""
    victim = net.account("v", 1_000)
    m1, m2 = net.account("m1"), net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    freeze_traced_funds(ledger, uetr, attribution=TRACED, reason="scam", placed_by="test")
    fund(m1, 500, key="unrelated")
    net.pay(m1, m2, 500)

    for attribution in BOTH:
        result = trace_payment(ledger, uetr, attribution=attribution)
        assert result.root.remaining_cents == 1_000
        assert result.root.secured_cents == 1_000
        assert result.hops == []


def test_a_released_hold_lets_the_traced_money_move_again(ledger, net):
    victim = net.account("v", 1_000)
    m1, m2 = net.account("m1"), net.account("m2")
    uetr = net.pay(victim, m1, 1_000)
    freeze = freeze_traced_funds(ledger, uetr, attribution=TRACED, reason="scam", placed_by="t")
    ledger.release_hold(freeze.holds[0].id, reason="cleared", released_by="test")
    net.pay(m1, m2, 1_000)

    result = trace_payment(ledger, uetr, attribution=TRACED)

    assert result.root.remaining_cents == 0
    assert by_account(result)[m2].traced_in_cents == 1_000
    assert result.secured_cents == 0


def test_an_unrelated_hold_pins_other_money_not_traced_money(ledger, net):
    victim = net.account("v", 1_000)
    m1 = net.account("m1", 500)  # its own money, all of it held for an unrelated reason
    m2 = net.account("m2")
    ledger.place_hold(m1, 500, reason="other case", placed_by="test")
    uetr = net.pay(victim, m1, 1_000)
    net.pay(m1, m2, 1_000)

    result = trace_payment(ledger, uetr, attribution=OWN)

    # The 500 of its own money cannot be spent, so the whole payment out is traced money.
    assert result.root.remaining_cents == 0
    assert by_account(result)[m2].traced_in_cents == 1_000


# -- stress ---------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_activity_keeps_every_trace_consistent_with_the_ledger(ledger, net, fund, seed):
    rng = random.Random(seed)
    accounts = [net.account(f"a{i}", 5_000) for i in range(8)]
    payments: list[str] = []

    for _ in range(160):
        action = rng.random()
        if action < 0.65:
            debtor, creditor = rng.sample(accounts, 2)
            try:
                payments.append(net.pay(debtor, creditor, rng.randint(1, 3_000)))
            except InsufficientFunds:
                pass
        elif action < 0.75:
            fund(rng.choice(accounts), rng.randint(1, 2_000), key=f"top-{rng.random()}")
        elif action < 0.88 and payments:
            freeze_traced_funds(
                ledger,
                rng.choice(payments),
                attribution=rng.choice(BOTH),
                reason="fuzz",
                placed_by="fuzz",
                max_depth=rng.randint(0, 6),
            )
        else:
            account = rng.choice(accounts)
            active = ledger.active_holds(account)
            if active:
                ledger.release_hold(rng.choice(active).id, reason="fuzz", released_by="fuzz")

    assert ledger.verify_invariants().ok
    assert payments
    for uetr in payments:
        for attribution in BOTH:
            result = trace_payment(ledger, uetr, attribution=attribution, max_depth=10)
            assert_conserved(result)
            seen = [node.account_id for node in result.nodes()]
            assert len(seen) == len(set(seen))
            assert result.nodes().__next__() is result.root
            for node in result.nodes():
                assert 0 <= node.remaining_cents <= ledger.balance(node.account_id)
                assert 0 <= node.secured_cents <= node.remaining_cents
                assert node.traced_in_cents == node.remaining_cents + node.moved_on_cents
