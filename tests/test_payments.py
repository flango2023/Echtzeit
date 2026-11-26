from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from helpers import BANK_A, BANK_B, T0, message

from echtzeit.errors import InvalidRequest, MessageError
from echtzeit.iso20022 import pacs002, pacs008
from echtzeit.iso20022.identifiers import make_german_iban, new_uetr
from echtzeit.models import SYSTEM
from echtzeit.payments import InstantPaymentProcessor

# -- accepted payments --------------------------------------------------------


def test_valid_payment_moves_money_and_is_accepted(processor, ledger, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)

    result = processor.process(pacs008.build(message(alice, bob)))

    assert result.accepted
    assert result.report.status == pacs002.ACCEPTED
    assert result.transfer is not None and not result.transfer.replayed
    assert ledger.balance(alice.id) == 4_000
    assert ledger.balance(bob.id) == 1_000
    assert ledger.verify_invariants().ok


def test_reply_is_a_parseable_pacs002_that_points_at_the_payment(processor, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)

    result = processor.process_transfer(original)
    reply = pacs002.parse(result.report_xml)

    assert reply == result.report
    assert reply.original_msg_id == original.msg_id
    assert reply.original_tx_id == original.tx_id
    assert reply.original_uetr == original.uetr
    assert reply.reporting_bic == BANK_B
    assert reply.debtor_agent_bic == BANK_A


def test_transfer_keeps_the_message_identifiers_for_tracing(processor, engine, customer):
    from sqlalchemy import text

    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)
    processor.process_transfer(original)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT uetr, end_to_end_id, reference FROM transfers WHERE uetr = :u"),
            {"u": original.uetr},
        ).one()
    assert row == (original.uetr, "E2E-1", "Invoice 42")


# -- rejections ---------------------------------------------------------------


@pytest.mark.parametrize("side", ["creditor", "debtor"])
def test_unknown_account_is_rejected_with_ac01(processor, ledger, customer, side):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    stranger = pacs008.Party("Nobody", make_german_iban("999999"), BANK_B)
    original = message(alice, bob)
    original = replace(original, **{side: stranger})

    result = processor.process_transfer(original)

    assert not result.accepted
    assert result.report.reason_code == "AC01"
    assert result.transfer is None
    assert ledger.balance(alice.id) == 5_000
    assert ledger.balance(bob.id) == 0


@pytest.mark.parametrize("side", ["debtor", "creditor"])
def test_wrong_bic_is_rejected_with_rc01(processor, ledger, customer, side):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)
    wrong = replace(getattr(original, side), bic="ECHTDEF2")
    original = replace(original, **{side: wrong})

    result = processor.process_transfer(original)

    assert result.report.reason_code == "RC01"
    assert ledger.balance(alice.id) == 5_000
    assert ledger.balance(bob.id) == 0


def test_insufficient_funds_is_rejected_and_nothing_moves(processor, ledger, customer):
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)

    result = processor.process_transfer(message(alice, bob, amount_cents=501))

    assert not result.accepted
    assert result.report.reason_code == "MS03"
    assert ledger.balance(alice.id) == 500
    assert ledger.balance(bob.id) == 0
    assert ledger.verify_invariants().ok


def test_exact_balance_can_be_sent(processor, ledger, customer):
    alice = customer("Alice", funds=1_000)
    bob = customer("Bob", bic=BANK_B)
    assert processor.process_transfer(message(alice, bob, amount_cents=1_000)).accepted
    assert ledger.balance(alice.id) == 0


# -- the ten-second limit -----------------------------------------------------


def test_message_exactly_at_the_limit_is_accepted(processor, clock, ledger, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    clock.now = T0 + timedelta(seconds=10)

    assert processor.process_transfer(message(alice, bob)).accepted
    assert ledger.balance(bob.id) == 1_000


def test_message_one_millisecond_over_the_limit_is_rejected_with_ab05(
    processor, clock, ledger, customer
):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    clock.now = T0 + timedelta(seconds=10, milliseconds=1)

    result = processor.process_transfer(message(alice, bob))

    assert result.report.reason_code == "AB05"
    assert ledger.balance(alice.id) == 5_000
    assert ledger.balance(bob.id) == 0


# -- repeated messages --------------------------------------------------------


def test_the_same_message_twice_moves_money_once(processor, ledger, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)

    first = processor.process_transfer(original)
    second = processor.process_transfer(original)

    assert first.accepted and second.accepted
    assert first.transfer.transfer_id == second.transfer.transfer_id
    assert second.transfer.replayed
    assert first.report.msg_id == second.report.msg_id
    assert ledger.balance(alice.id) == 4_000
    assert ledger.balance(bob.id) == 1_000


def test_a_late_retry_of_a_completed_payment_is_still_accepted(processor, clock, ledger, customer):
    """The retry arrives after the limit. The money already moved, so 'timed out' would be false."""
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)
    assert processor.process_transfer(original).accepted

    clock.now = T0 + timedelta(minutes=5)
    retry = processor.process_transfer(original)

    assert retry.accepted
    assert retry.transfer.replayed
    assert ledger.balance(bob.id) == 1_000


def test_same_tx_id_with_different_content_is_rejected_as_duplicate(processor, ledger, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    assert processor.process_transfer(message(alice, bob)).accepted

    result = processor.process_transfer(message(alice, bob, amount_cents=2_000))

    assert result.report.reason_code == "AM05"
    assert ledger.balance(alice.id) == 4_000
    assert ledger.balance(bob.id) == 1_000


def test_tx_id_is_scoped_to_the_originator_bank(processor, ledger, customer):
    alice = customer("Alice", bic=BANK_A, funds=5_000)
    carol = customer("Carol", bic="ECHTDEF2", funds=5_000)
    bob = customer("Bob", bic=BANK_B)

    first = processor.process_transfer(message(alice, bob, tx_id="TX-SAME"))
    second = processor.process_transfer(message(carol, bob, tx_id="TX-SAME", uetr=new_uetr()))

    assert first.accepted and second.accepted
    assert ledger.balance(bob.id) == 2_000


def test_a_rejected_payment_can_succeed_when_resent_after_funding(
    processor, ledger, customer, bank
):
    alice = customer("Alice", funds=100)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)
    rejected = processor.process_transfer(original)
    assert rejected.report.reason_code == "MS03"

    ledger.transfer(
        idempotency_key="top-up", debtor_id=bank, creditor_id=alice.id, amount_cents=5_000
    )
    accepted = processor.process_transfer(original)

    assert accepted.accepted
    assert accepted.report.msg_id != rejected.report.msg_id


def test_the_same_message_sent_in_parallel_moves_money_once(processor, ledger, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: processor.process_transfer(original), range(32)))

    assert all(r.accepted for r in results)
    assert len({r.transfer.transfer_id for r in results}) == 1
    assert ledger.balance(alice.id) == 4_000
    assert ledger.balance(bob.id) == 1_000
    assert ledger.verify_invariants().ok


# -- input that cannot be answered -------------------------------------------


@pytest.mark.parametrize("garbage", [b"", b"not xml", b"<Document/>"])
def test_unparseable_message_raises_and_moves_nothing(processor, ledger, customer, garbage):
    alice = customer("Alice", funds=5_000)
    with pytest.raises(MessageError):
        processor.process(garbage)
    assert ledger.balance(alice.id) == 5_000


# -- account identifiers ------------------------------------------------------


def test_account_creation_validates_identifiers(ledger):
    good = make_german_iban("1")
    with pytest.raises(InvalidRequest, match="iban is not valid"):
        ledger.create_account("A", iban="DE00", bic=BANK_A)
    with pytest.raises(InvalidRequest, match="bic is not valid"):
        ledger.create_account("A", iban=good, bic="bad")
    with pytest.raises(InvalidRequest, match="together"):
        ledger.create_account("A", iban=good)

    ledger.create_account("A", iban=good, bic=BANK_A)
    with pytest.raises(InvalidRequest, match="already in use"):
        ledger.create_account("B", iban=good, bic=BANK_A)


def test_system_account_cannot_be_reached_by_a_payment_message(ledger):
    with pytest.raises(InvalidRequest, match="system account"):
        ledger.create_account("Funding", kind=SYSTEM, iban=make_german_iban("1"), bic=BANK_A)


# -- incoming screen --------------------------------------------------------------------------


def test_a_payment_held_back_by_the_payees_bank_moves_nothing(ledger, audit, clock, customer):
    calls = []

    def hold_everything(message, creditor, now):
        calls.append((message.tx_id, creditor.id))
        return True

    screened = InstantPaymentProcessor(
        ledger, audit=audit, clock=clock, incoming_screen=hold_everything
    )
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)

    result = screened.process_transfer(message(alice, bob))

    assert result.report.reason_code == "RR04"
    assert calls == [("TX-1", bob.id)]
    assert ledger.balance(alice.id) == 5_000 and ledger.balance(bob.id) == 0
    assert [r.reason_code for r in audit.rejections()] == ["RR04"]


def test_a_completed_payment_is_not_screened_again_on_retry(ledger, audit, clock, customer):
    decisions = iter([False, True])

    def screen(_message, _creditor, _now):
        return next(decisions)

    screened = InstantPaymentProcessor(ledger, audit=audit, clock=clock, incoming_screen=screen)
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)

    assert screened.process_transfer(original).accepted
    retry = screened.process_transfer(original)

    assert retry.accepted and retry.transfer.replayed
    assert ledger.balance(bob.id) == 1_000


def test_a_message_dated_in_the_future_is_refused_like_a_late_one(processor, customer, clock):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    future = message(alice, bob, accepted_at=T0 + timedelta(days=30))
    assert processor.process_transfer(future).report.reason_code == "AB05"


def test_a_resend_under_a_new_msg_id_gets_its_own_report_ids(processor, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)
    first = processor.process_transfer(original)
    again = processor.process_transfer(original)  # the same message: the same reply
    resent = processor.process_transfer(replace(original, msg_id="MSG-9"))
    assert first.report.msg_id == again.report.msg_id
    assert resent.accepted and resent.report.msg_id != first.report.msg_id
    assert resent.transfer.transfer_id == first.transfer.transfer_id  # money moved once
