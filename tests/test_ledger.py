import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from echtzeit.errors import (
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidRequest,
)


def test_transfer_moves_money_and_writes_two_balanced_entries(ledger, fund, engine):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 10_000)

    result = ledger.transfer(
        idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=1_000
    )

    assert result.replayed is False
    assert ledger.balance(alice) == 9_000
    assert ledger.balance(bob) == 1_000
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT account_id, amount_cents FROM ledger_entries WHERE transfer_id = :t"),
            {"t": result.transfer_id},
        ).all()
    assert sorted(r[1] for r in rows) == [-1_000, 1_000]
    assert ledger.verify_invariants().ok


def test_insufficient_funds_changes_nothing(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 500)

    with pytest.raises(InsufficientFunds):
        ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=501)

    assert ledger.balance(alice) == 500
    assert ledger.balance(bob) == 0
    assert ledger.verify_invariants().ok


def test_rejected_transfer_can_be_retried_with_same_key_after_funding(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    with pytest.raises(InsufficientFunds):
        ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=100)

    fund(alice, 100)
    result = ledger.transfer(
        idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=100
    )

    assert result.replayed is False
    assert ledger.balance(bob) == 100


def test_exact_balance_can_be_spent(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 250)
    ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=250)
    assert ledger.balance(alice) == 0
    assert ledger.balance(bob) == 250


def test_system_account_goes_negative_by_exactly_what_it_issued(ledger, bank, fund):
    alice = ledger.create_account("Alice")
    fund(alice, 700)
    assert ledger.balance(bank) == -700
    assert ledger.verify_invariants().ok


def test_same_key_same_payload_replays_without_moving_money_again(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 5_000)
    kwargs = {
        "idempotency_key": "t-1",
        "debtor_id": alice,
        "creditor_id": bob,
        "amount_cents": 1_000,
    }

    first = ledger.transfer(**kwargs)
    second = ledger.transfer(**kwargs)

    assert first.transfer_id == second.transfer_id
    assert first.replayed is False
    assert second.replayed is True
    assert ledger.balance(alice) == 4_000
    assert ledger.balance(bob) == 1_000


def test_same_key_different_payload_is_a_conflict(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 5_000)
    ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=1_000)

    with pytest.raises(IdempotencyConflict):
        ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=2_000)

    assert ledger.balance(bob) == 1_000


@pytest.mark.parametrize("amount", [0, -1, 1.5, "10", None, True])
def test_invalid_amounts_are_rejected(ledger, amount):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    with pytest.raises(InvalidRequest):
        ledger.transfer(
            idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=amount
        )


def test_same_debtor_and_creditor_is_rejected(ledger):
    alice = ledger.create_account("Alice")
    with pytest.raises(InvalidRequest):
        ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=alice, amount_cents=1)


@pytest.mark.parametrize("key", ["", "   ", None])
def test_empty_idempotency_key_is_rejected(ledger, key):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    with pytest.raises(InvalidRequest):
        ledger.transfer(idempotency_key=key, debtor_id=alice, creditor_id=bob, amount_cents=1)


def test_unknown_account_is_rejected_and_nothing_changes(ledger, fund):
    alice = ledger.create_account("Alice")
    fund(alice, 1_000)
    with pytest.raises(AccountNotFound):
        ledger.transfer(
            idempotency_key="t-1", debtor_id=alice, creditor_id="does-not-exist", amount_cents=100
        )
    assert ledger.balance(alice) == 1_000
    assert ledger.verify_invariants().ok


def test_failure_after_balance_update_rolls_everything_back(ledger, fund, monkeypatch):
    """Atomicity: if writing the ledger entries fails, the balance updates must not persist."""
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 5_000)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash while writing ledger entries")

    monkeypatch.setattr(type(ledger), "_write_entries", staticmethod(boom))

    with pytest.raises(RuntimeError):
        ledger.transfer(idempotency_key="t-1", debtor_id=alice, creditor_id=bob, amount_cents=1_000)

    monkeypatch.undo()
    assert ledger.balance(alice) == 5_000
    assert ledger.balance(bob) == 0
    assert ledger.verify_invariants().ok


def test_ledger_entries_and_transfers_are_append_only(ledger, fund, engine):
    alice = ledger.create_account("Alice")
    fund(alice, 1_000)

    for statement in (
        "UPDATE ledger_entries SET amount_cents = 1",
        "DELETE FROM ledger_entries",
        "UPDATE transfers SET amount_cents = 1",
        "DELETE FROM transfers",
    ):
        with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
            conn.execute(text(statement))

    assert ledger.verify_invariants().ok


def test_verify_invariants_detects_a_tampered_balance(ledger, fund, engine):
    alice = ledger.create_account("Alice")
    fund(alice, 1_000)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE accounts SET balance_cents = balance_cents + 1 WHERE id = :i"),
            {"i": alice},
        )

    report = ledger.verify_invariants()

    assert not report.ok
    assert any("differs from ledger sum" in p for p in report.problems)


def test_different_requests_never_share_an_idempotency_hash(ledger, fund):
    """Field boundaries are part of the hash, so moving a separator changes it."""
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 5_000)
    ledger.transfer(
        idempotency_key="t-sep",
        debtor_id=alice,
        creditor_id=bob,
        amount_cents=100,
        reference="x|y",
        end_to_end_id="z",
    )
    with pytest.raises(IdempotencyConflict):
        ledger.transfer(
            idempotency_key="t-sep",
            debtor_id=alice,
            creditor_id=bob,
            amount_cents=100,
            reference="x",
            end_to_end_id="y|z",
        )
    assert ledger.balance(bob) == 100
