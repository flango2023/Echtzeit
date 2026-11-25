"""Concurrency tests.

These run real threads against a file-based SQLite database. SQLite serializes
writers, so these tests prove the ledger rules hold when requests overlap. They do
not prove PostgreSQL row-lock behaviour.
"""

import random
from concurrent.futures import ThreadPoolExecutor

from echtzeit.errors import InsufficientFunds


def test_parallel_spends_never_overdraw_an_account(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 1_000)  # enough for exactly 10 transfers of 100

    def attempt(i: int) -> str:
        try:
            ledger.transfer(
                idempotency_key=f"spend-{i}", debtor_id=alice, creditor_id=bob, amount_cents=100
            )
            return "ok"
        except InsufficientFunds:
            return "rejected"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(40)))

    assert outcomes.count("ok") == 10
    assert outcomes.count("rejected") == 30
    assert ledger.balance(alice) == 0
    assert ledger.balance(bob) == 1_000
    assert ledger.verify_invariants().ok


def test_the_same_idempotency_key_sent_in_parallel_moves_money_once(ledger, fund):
    alice = ledger.create_account("Alice")
    bob = ledger.create_account("Bob")
    fund(alice, 10_000)

    def send(_):
        return ledger.transfer(
            idempotency_key="same-key", debtor_id=alice, creditor_id=bob, amount_cents=1_000
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(send, range(32)))

    assert len({r.transfer_id for r in results}) == 1
    assert sum(1 for r in results if not r.replayed) == 1
    assert ledger.balance(alice) == 9_000
    assert ledger.balance(bob) == 1_000
    assert ledger.verify_invariants().ok


def test_random_parallel_transfers_conserve_total_money(ledger, bank, fund):
    accounts = [ledger.create_account(f"Customer {i}") for i in range(6)]
    for account in accounts:
        fund(account, 5_000)
    total_before = sum(ledger.balance(a) for a in accounts)
    rng = random.Random(42)
    plan = []
    for i in range(300):
        debtor, creditor = rng.sample(accounts, 2)
        plan.append((f"rand-{i}", debtor, creditor, rng.randint(1, 2_000)))

    def run(item):
        key, debtor, creditor, amount = item
        try:
            ledger.transfer(
                idempotency_key=key, debtor_id=debtor, creditor_id=creditor, amount_cents=amount
            )
        except InsufficientFunds:
            pass

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(run, plan))

    assert sum(ledger.balance(a) for a in accounts) == total_before
    assert all(ledger.balance(a) >= 0 for a in accounts)
    assert ledger.verify_invariants().ok
