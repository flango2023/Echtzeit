from concurrent.futures import ThreadPoolExecutor

import pytest
from helpers import BANK_B, message
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from echtzeit.errors import (
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidRequest,
)
from echtzeit.iso20022.identifiers import new_uetr


@pytest.fixture
def pair(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 1_000)
    return alice, bob


def pay(ledger, debtor, creditor, cents, key, **extra):
    return ledger.transfer(
        idempotency_key=key, debtor_id=debtor, creditor_id=creditor, amount_cents=cents, **extra
    )


def test_hold_reduces_spendable_money_but_not_the_balance(ledger, pair):
    alice, _ = pair
    hold = ledger.place_hold(alice, 400, reason="reported scam", placed_by="test")

    assert hold.amount_cents == 400 and hold.requested_cents == 400
    assert ledger.balance(alice) == 1_000
    assert ledger.available_balance(alice) == 600
    assert [h.id for h in ledger.active_holds(alice)] == [hold.id]
    assert ledger.verify_invariants().ok


def test_held_money_cannot_be_spent(ledger, pair):
    alice, bob = pair
    ledger.place_hold(alice, 400, reason="r", placed_by="test")

    with pytest.raises(InsufficientFunds):
        pay(ledger, alice, bob, 601, "t-1")
    assert pay(ledger, alice, bob, 600, "t-2").replayed is False
    assert ledger.available_balance(alice) == 0
    assert ledger.balance(alice) == 400
    assert ledger.verify_invariants().ok


def test_money_can_still_arrive_on_a_held_account(ledger, pair, fund):
    alice, _ = pair
    ledger.place_hold(alice, 1_000, reason="r", placed_by="test")
    fund(alice, 250, key="more")
    assert ledger.available_balance(alice) == 250


def test_a_hold_cannot_exceed_spendable_money(ledger, pair):
    alice, _ = pair
    ledger.place_hold(alice, 700, reason="r", placed_by="test")
    with pytest.raises(InsufficientFunds):
        ledger.place_hold(alice, 301, reason="r", placed_by="test")
    ledger.place_hold(alice, 300, reason="r", placed_by="test")
    assert ledger.available_balance(alice) == 0


def test_release_gives_the_money_back_to_the_owner(ledger, pair):
    alice, bob = pair
    hold = ledger.place_hold(alice, 1_000, reason="r", placed_by="test")
    with pytest.raises(InsufficientFunds):
        pay(ledger, alice, bob, 1, "t-1")

    ledger.release_hold(hold.id, reason="false alarm", released_by="test")

    assert ledger.active_holds(alice) == []
    assert ledger.available_balance(alice) == 1_000
    pay(ledger, alice, bob, 1_000, "t-2")
    assert ledger.verify_invariants().ok


def test_a_hold_cannot_be_released_twice_or_unknown(ledger, pair):
    alice, _ = pair
    hold = ledger.place_hold(alice, 100, reason="r", placed_by="test")
    ledger.release_hold(hold.id, reason="r", released_by="test")
    with pytest.raises(InvalidRequest, match="already released"):
        ledger.release_hold(hold.id, reason="r", released_by="test")
    with pytest.raises(InvalidRequest, match="unknown hold"):
        ledger.release_hold("no-such-hold", reason="r", released_by="test")
    assert ledger.available_balance(alice) == 1_000


@pytest.mark.parametrize("amount", [0, -5, 1.5, "10", True, None])
def test_hold_amount_must_be_a_positive_integer(ledger, pair, amount):
    alice, _ = pair
    with pytest.raises(InvalidRequest):
        ledger.place_hold(alice, amount, reason="r", placed_by="test")


@pytest.mark.parametrize(
    ("reason", "placed_by"), [("", "t"), ("  ", "t"), ("r", ""), ("x" * 201, "t"), ("r", "x" * 101)]
)
def test_hold_needs_a_reason_and_an_actor(ledger, pair, reason, placed_by):
    alice, _ = pair
    with pytest.raises(InvalidRequest):
        ledger.place_hold(alice, 10, reason=reason, placed_by=placed_by)


def test_unknown_account_and_system_account_cannot_be_held(ledger, bank, pair):
    with pytest.raises(AccountNotFound):
        ledger.place_hold("missing", 10, reason="r", placed_by="t")
    with pytest.raises(InvalidRequest, match="system account"):
        ledger.place_hold(bank, 10, reason="r", placed_by="t")
    with pytest.raises(InvalidRequest, match="system account"):
        ledger.freeze_available(bank, 10, reason="r", placed_by="t")


def test_freeze_available_secures_what_it_can_and_records_the_request(ledger, pair):
    alice, bob = pair
    pay(ledger, alice, bob, 700, "t-1")  # 300 left

    hold = ledger.freeze_available(alice, 1_000, reason="scam", placed_by="test")

    assert hold.requested_cents == 1_000
    assert hold.amount_cents == 300
    assert ledger.available_balance(alice) == 0


def test_freeze_available_with_nothing_spendable_returns_none(ledger, pair):
    alice, bob = pair
    pay(ledger, alice, bob, 1_000, "t-1")
    assert ledger.freeze_available(alice, 500, reason="scam", placed_by="test") is None
    assert ledger.active_holds(alice) == []


def test_freeze_available_does_not_hold_more_than_requested(ledger, pair):
    alice, _ = pair
    hold = ledger.freeze_available(alice, 200, reason="scam", placed_by="test")
    assert hold.amount_cents == 200 and hold.requested_cents == 200


def test_invariants_detect_a_held_cache_that_does_not_match_the_holds(ledger, pair, engine):
    alice, _ = pair
    ledger.place_hold(alice, 100, reason="r", placed_by="test")
    with engine.begin() as conn:
        conn.execute(text("UPDATE accounts SET held_cents = 50 WHERE id = :i"), {"i": alice})

    report = ledger.verify_invariants()

    assert not report.ok
    assert any("differs from active holds" in problem for problem in report.problems)


def test_database_refuses_more_held_than_the_balance(ledger, pair, engine):
    alice, _ = pair
    with pytest.raises(DatabaseError), engine.begin() as conn:
        conn.execute(text("UPDATE accounts SET held_cents = 2000 WHERE id = :i"), {"i": alice})


def test_holds_and_releases_are_append_only(ledger, pair, engine):
    alice, _ = pair
    hold = ledger.place_hold(alice, 100, reason="r", placed_by="test")
    ledger.release_hold(hold.id, reason="r", released_by="test")

    for statement in (
        "UPDATE holds SET amount_cents = 1",
        "DELETE FROM holds",
        "UPDATE hold_releases SET reason = 'x'",
        "DELETE FROM hold_releases",
    ):
        with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
            conn.execute(text(statement))


def test_parallel_spending_and_freezing_never_overcommit_the_balance(ledger, pair):
    alice, bob = pair  # 1,000 available

    def act(i: int):
        if i % 2 == 0:
            try:
                pay(ledger, alice, bob, 100, f"spend-{i}")
                return ("spent", 100)
            except InsufficientFunds:
                return ("refused", 0)
        hold = ledger.freeze_available(alice, 100, reason="scam", placed_by=f"t{i}")
        return ("held", 0 if hold is None else hold.amount_cents)

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(act, range(40)))

    spent = sum(amount for kind, amount in outcomes if kind == "spent")
    held = sum(amount for kind, amount in outcomes if kind == "held")
    assert spent + held <= 1_000
    assert ledger.balance(alice) == 1_000 - spent
    assert ledger.available_balance(alice) == 1_000 - spent - held
    assert ledger.verify_invariants().ok


# -- how holds interact with payment messages -------------------------------------------


def test_a_payment_message_cannot_spend_held_money(processor, ledger, audit, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    ledger.place_hold(alice.id, 4_500, reason="r", placed_by="test")

    result = processor.process_transfer(message(alice, bob, amount_cents=600))

    assert result.report.reason_code == "MS03"
    assert [r.reason_code for r in audit.rejections()] == ["MS03"]
    assert ledger.balance(bob.id) == 0


def test_the_same_uetr_under_another_key_is_a_conflict(ledger, pair):
    alice, bob = pair
    uetr = new_uetr()
    pay(ledger, alice, bob, 100, "t-1", uetr=uetr)

    with pytest.raises(IdempotencyConflict, match="uetr"):
        pay(ledger, alice, bob, 100, "t-2", uetr=uetr)
    assert ledger.balance(bob) == 100


def test_a_message_with_a_used_uetr_is_rejected_as_duplicate(processor, ledger, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    first = message(alice, bob)
    assert processor.process_transfer(first).accepted

    again = message(alice, bob, tx_id="TX-OTHER", uetr=first.uetr)
    result = processor.process_transfer(again)

    assert result.report.reason_code == "AM05"
    assert ledger.balance(bob.id) == 1_000
