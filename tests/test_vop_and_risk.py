from datetime import UTC, datetime, timedelta

import pytest

from echtzeit.iso20022.identifiers import make_german_iban, new_uetr
from echtzeit.risk import Decision, RiskConfig, RiskScorer, recent_incoming_uetrs
from echtzeit.vop import VopOutcome, compare_names, normalise, verify_payee

T = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
BIC = "ECHTDEFF"


# -- Verification of Payee ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "holder", "outcome"),
    [
        ("Anna Schmidt", "Anna Schmidt", VopOutcome.MATCH),
        ("schmidt, anna", "Anna Schmidt", VopOutcome.MATCH),  # order, case, punctuation
        ("Jürgen Groß", "Jurgen Gross", VopOutcome.MATCH),  # accents and ß
        ("Anna Schmitt", "Anna Schmidt", VopOutcome.CLOSE_MATCH),  # one-letter typo
        ("Muster Lieferant GmbH", "Max Weber", VopOutcome.NO_MATCH),
        ("", "Max Weber", VopOutcome.NO_MATCH),
        ("!!!", "Max Weber", VopOutcome.NO_MATCH),
    ],
)
def test_name_comparison(typed, holder, outcome):
    assert compare_names(typed, holder)[0] is outcome


def test_normalise_is_stable():
    assert normalise("  Groß-Müller,  Jörg ") == normalise("jorg gross muller")


def test_verify_payee_against_the_ledger(ledger):
    iban = make_german_iban("1")
    ledger.create_account("Anna Schmidt", iban=iban, bic=BIC)

    assert verify_payee(ledger, iban, "Anna Schmidt").outcome is VopOutcome.MATCH
    close = verify_payee(ledger, iban, "Anna Schmitt")
    assert close.outcome is VopOutcome.CLOSE_MATCH and close.suggested_name == "Anna Schmidt"
    no = verify_payee(ledger, iban, "Lieferant GmbH")
    assert no.outcome is VopOutcome.NO_MATCH and no.suggested_name is None
    missing = verify_payee(ledger, make_german_iban("2"), "Anna Schmidt")
    assert missing.outcome is VopOutcome.NOT_POSSIBLE


# -- risk score -----------------------------------------------------------------------------


@pytest.fixture
def world(ledger, bank):
    counter = iter(range(1, 100))

    def account(name: str, funds: int = 0):
        iban = make_german_iban(str(next(counter)))
        account_id = ledger.create_account(name, iban=iban, bic=BIC)
        if funds:
            ledger.transfer(
                idempotency_key=f"fund-{account_id}",
                debtor_id=bank,
                creditor_id=account_id,
                amount_cents=funds,
                booked_at=T - timedelta(days=30),
            )
        return account_id, iban

    keys = iter(range(1, 10_000))

    def pay(debtor, creditor, cents, at):
        uetr = new_uetr()
        ledger.transfer(
            idempotency_key=f"k-{next(keys)}",
            debtor_id=debtor,
            creditor_id=creditor,
            amount_cents=cents,
            uetr=uetr,
            booked_at=at,
        )
        return uetr

    return account, pay


def test_a_routine_payment_to_a_known_payee_is_allowed(ledger, world):
    account, pay = world
    alice, _ = account("Alice", 500_000)
    shop, shop_iban = account("Shop")
    pay(alice, shop, 5_000, T - timedelta(days=3))

    result = RiskScorer(ledger).assess(
        debtor_id=alice, creditor_iban=shop_iban, typed_name="Shop", amount_cents=6_000, at=T
    )

    assert result.decision is Decision.ALLOW
    assert result.points == 0 and result.reasons == []
    assert result.vop.outcome is VopOutcome.MATCH
    assert result.elapsed_ns > 0


def test_a_safe_account_pattern_is_blocked_with_every_reason_listed(ledger, world):
    account, pay = world
    victim, _ = account("Victim", 1_000_000)
    shop, _ = account("Shop")
    pay(victim, shop, 5_000, T - timedelta(days=3))
    _mule, mule_iban = account("Max Weber")

    result = RiskScorer(ledger).assess(
        debtor_id=victim,
        creditor_iban=mule_iban,
        typed_name="Max Weber",
        amount_cents=900_000,
        at=T,
    )

    assert {r.rule for r in result.reasons} == {
        "NEW_PAYEE",
        "AMOUNT_SPIKE",
        "BALANCE_DRAIN",
        "FRESH_RECEIVER",
    }
    assert result.points == 80
    assert result.decision is Decision.BLOCK


def test_a_changed_invoice_iban_gives_a_vop_no_match(ledger, world):
    account, pay = world
    sme, _ = account("Baeckerei Example", 2_000_000)
    supplier, _ = account("Muster Lieferant GmbH")
    for week in range(1, 5):
        pay(sme, supplier, 120_000, T - timedelta(weeks=week))
    mule, mule_iban = account("Max Weber")
    pay(account("Someone", 10_000)[0], mule, 100, T - timedelta(days=60))  # an established account

    result = RiskScorer(ledger).assess(
        debtor_id=sme,
        creditor_iban=mule_iban,
        typed_name="Muster Lieferant GmbH",
        amount_cents=130_000,
        at=T,
    )

    assert result.fired("VOP_NO_MATCH") and result.fired("NEW_PAYEE")
    assert not result.fired("FRESH_RECEIVER") and not result.fired("AMOUNT_SPIKE")
    assert result.points == 60
    assert result.decision is Decision.WARN


def test_money_passing_straight_through_is_flagged(ledger, world):
    account, pay = world
    victim, _ = account("Victim", 500_000)
    mule, _ = account("Mule")
    _other, other_iban = account("Mule Two")
    pay(victim, mule, 400_000, T - timedelta(minutes=10))

    result = RiskScorer(ledger).assess(
        debtor_id=mule, creditor_iban=other_iban, typed_name="Mule Two", amount_cents=380_000, at=T
    )

    assert result.fired("PASS_THROUGH")
    assert result.decision is Decision.BLOCK
    assert [
        amount for _, amount in recent_incoming_uetrs(ledger, mule, at=T, window=timedelta(hours=1))
    ] == [400_000]


def test_pass_through_ignores_old_money_and_system_funding(ledger, world):
    account, pay = world
    victim, _ = account("Victim", 500_000)
    saver, _ = account("Saver", 400_000)  # funded by the system account
    _payee, payee_iban = account("Payee")
    pay(victim, saver, 1_000, T - timedelta(hours=3))

    result = RiskScorer(ledger).assess(
        debtor_id=saver, creditor_iban=payee_iban, typed_name="Payee", amount_cents=380_000, at=T
    )

    assert not result.fired("PASS_THROUGH")


def test_only_earlier_history_counts(ledger, world):
    """A payment booked after the assessment moment must not make the payee look known."""
    account, pay = world
    alice, _ = account("Alice", 500_000)
    shop, shop_iban = account("Shop")
    pay(alice, shop, 5_000, T + timedelta(minutes=5))

    result = RiskScorer(ledger).assess(
        debtor_id=alice, creditor_iban=shop_iban, typed_name="Shop", amount_cents=5_000, at=T
    )

    assert result.fired("NEW_PAYEE") and result.fired("FRESH_RECEIVER")


def test_thresholds_come_from_the_config(ledger, world):
    account, _ = world
    alice, _ = account("Alice", 500_000)
    _, shop_iban = account("Shop")
    strict = RiskScorer(ledger, RiskConfig(warn_at=10, block_at=30))

    result = strict.assess(
        debtor_id=alice, creditor_iban=shop_iban, typed_name="Shop", amount_cents=1_000, at=T
    )

    assert result.points == 35  # new payee and fresh receiver
    assert result.decision is Decision.BLOCK
