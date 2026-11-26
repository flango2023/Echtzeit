from dataclasses import replace
from datetime import timedelta

import pytest
from helpers import BANK_A, BANK_B, T0, message
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from echtzeit.audit import AuditLog
from echtzeit.iso20022 import pacs002, pacs008
from echtzeit.iso20022.identifiers import make_german_iban, new_uetr


def test_accepted_payment_writes_nothing_to_the_audit_log(processor, audit, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)

    assert processor.process_transfer(message(alice, bob)).accepted

    assert audit.rejections() == []


def test_rejection_is_recorded_with_the_message_and_reply_details(
    processor, audit, clock, customer
):
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob, amount_cents=501)

    result = processor.process_transfer(original)

    (record,) = audit.rejections()
    assert record.recorded_at.replace(tzinfo=None) == clock.now.replace(tzinfo=None)
    assert record.msg_id == original.msg_id
    assert record.tx_id == original.tx_id
    assert record.uetr == original.uetr
    assert record.end_to_end_id == original.end_to_end_id
    assert record.debtor_iban == alice.iban
    assert record.creditor_iban == bob.iban
    assert record.debtor_bic == BANK_A
    assert record.creditor_bic == BANK_B
    assert record.amount_cents == 501
    assert record.reason_code == "MS03"
    assert record.reply_msg_id == result.report.msg_id


def test_each_rejection_reason_is_recorded(processor, audit, clock, customer):
    alice = customer("Alice", funds=5_000)
    bob = customer("Bob", bic=BANK_B)
    stranger = pacs008.Party("Nobody", make_german_iban("999999"), BANK_B)
    wrong_bic = replace(message(alice, bob).creditor, bic="ECHTDEF2")

    processor.process_transfer(replace(message(alice, bob), creditor=stranger))  # AC01
    processor.process_transfer(
        replace(message(alice, bob, tx_id="TX-2", uetr=new_uetr()), creditor=wrong_bic)
    )  # RC01
    processor.process_transfer(message(alice, bob, tx_id="TX-3", amount_cents=6_000))  # MS03
    assert processor.process_transfer(message(alice, bob, tx_id="TX-4")).accepted
    processor.process_transfer(message(alice, bob, tx_id="TX-4", amount_cents=2))  # AM05
    clock.now = T0 + timedelta(seconds=11)
    processor.process_transfer(message(alice, bob, tx_id="TX-5", uetr=new_uetr()))  # AB05

    assert audit.count_by_reason() == {"AC01": 1, "RC01": 1, "MS03": 1, "AM05": 1, "AB05": 1}


def test_unknown_account_attempts_keep_the_iban_that_was_tried(processor, audit, customer):
    alice = customer("Alice", funds=500)
    probed = make_german_iban("424242")
    stranger = pacs008.Party("Nobody", probed, BANK_B)

    processor.process_transfer(
        replace(message(alice, customer("Bob", bic=BANK_B)), creditor=stranger)
    )

    (record,) = audit.rejections(reason_code="AC01")
    assert record.creditor_iban == probed


def test_every_attempt_is_recorded_so_repeated_probing_is_visible(processor, audit, customer):
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob, amount_cents=501)

    for _ in range(3):
        processor.process_transfer(original)

    assert len(audit.rejections()) == 3
    assert len({r.reply_msg_id for r in audit.rejections()}) == 1


def test_filtering_by_reason_code(processor, audit, customer):
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)
    processor.process_transfer(message(alice, bob, amount_cents=501))

    assert len(audit.rejections(reason_code="MS03")) == 1
    assert audit.rejections(reason_code="AC01") == []


def test_audit_rows_cannot_be_changed_or_deleted(processor, audit, engine, customer):
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)
    processor.process_transfer(message(alice, bob, amount_cents=501))

    for statement in (
        "UPDATE rejected_payments SET reason_code = 'XXXX'",
        "DELETE FROM rejected_payments",
    ):
        # Updates are always refused; deletes until the retention period ends (none is set).
        with (
            pytest.raises(DatabaseError, match="append-only|retention period ends"),
            engine.begin() as conn,
        ):
            conn.execute(text(statement))

    assert len(audit.rejections()) == 1


def test_a_failed_audit_write_stops_the_reply(processor, audit, ledger, customer, monkeypatch):
    """Fail closed: no reply without an audit record. No money moved, so a retry is safe."""
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)

    def broken(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(AuditLog, "record_rejection", broken)
    with pytest.raises(RuntimeError, match="audit store"):
        processor.process_transfer(message(alice, bob, amount_cents=501))
    monkeypatch.undo()

    assert audit.rejections() == []
    assert ledger.balance(alice.id) == 500
    assert ledger.verify_invariants().ok


def test_only_rejected_reports_can_be_recorded(audit, customer):
    alice = customer("Alice", funds=500)
    bob = customer("Bob", bic=BANK_B)
    original = message(alice, bob)
    accepted = pacs002.report_for(original, msg_id="R-1", status_id="S-1", created_at=T0)

    with pytest.raises(ValueError, match="only rejected"):
        audit.record_rejection(original, accepted, T0)
