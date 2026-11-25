"""Turn a pacs.008 instant credit transfer into a ledger transfer and answer with a pacs.002.

This simulates both PSPs in one process. The checks run in this order, and the first one
that fails produces a rejection:

1. The debtor and creditor IBANs must belong to known accounts. Otherwise AC01.
2. The BICs in the message must match the BICs registered for those accounts. Otherwise RC01.
3. A request already completed under the same key is answered as accepted again, whatever its
   age. The same key with different content is rejected as AM05 (duplicate).
4. A message older than the execution time limit is rejected as AB05, and no money moves.
5. The debtor must cover the amount. Otherwise MS03.

Step 3 comes before step 4 on purpose. If a payment was completed and the originator retries
after the time limit, answering "timed out" would tell the originator that the payment failed
while the money has moved. That mismatch is the main hazard of timeouts in instant payments.

About the reason codes: the EPC guidelines list no code for "insufficient funds" in the
inter-PSP rejection, because a real originator PSP refuses such a payment before it creates a
pacs.008. This simulator models both sides, so it needs some answer, and it uses MS03 (reason
not specified, agent generated). That mapping is a modelling choice of this project.

The idempotency key is "<debtor BIC>:<TxId>", because the guidelines make TxId unique per
originator PSP, not globally.

Every rejection is written to the audit log before the reply is returned.

A message that cannot be parsed has no identifiers to answer, so `process` raises
MessageError and produces no pacs.002. Such a message is not written to the audit log
either, because there is nothing reliable to store.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from echtzeit.audit import AuditLog
from echtzeit.errors import IdempotencyConflict, InsufficientFunds
from echtzeit.iso20022 import pacs002, pacs008
from echtzeit.ledger import Ledger, TransferResult

# The Instant Payments Regulation and the SCT Inst scheme require funds to be available
# to the beneficiary within 10 seconds.
EXECUTION_TIMEOUT = timedelta(seconds=10)

UNKNOWN_ACCOUNT = "AC01"
WRONG_BANK_IDENTIFIER = "RC01"
DUPLICATE = "AM05"
TIMEOUT = "AB05"
NOT_SPECIFIED = "MS03"


@dataclass(frozen=True)
class ProcessingResult:
    report: pacs002.StatusReport
    report_xml: bytes
    transfer: TransferResult | None  # set only when the payment is accepted

    @property
    def accepted(self) -> bool:
        return self.report.status == pacs002.ACCEPTED


def _derived_id(prefix: str, key: str) -> str:
    """A stable identifier of at most 35 characters, derived from the seed."""
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:30]}"


class InstantPaymentProcessor:
    def __init__(
        self,
        ledger: Ledger,
        *,
        audit: AuditLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: timedelta = EXECUTION_TIMEOUT,
    ):
        self._ledger = ledger
        self._audit = audit
        self._clock = clock
        self._timeout = timeout

    def process(self, xml: bytes | str) -> ProcessingResult:
        """Parse a pacs.008 and process it. Raises MessageError if it cannot be parsed."""
        return self.process_transfer(pacs008.parse(xml))

    def process_transfer(self, message: pacs008.CreditTransfer) -> ProcessingResult:
        now = self._clock()
        key = f"{message.debtor.bic}:{message.tx_id}"

        debtor = self._ledger.find_account_by_iban(message.debtor.iban)
        creditor = self._ledger.find_account_by_iban(message.creditor.iban)
        if debtor is None or creditor is None:
            return self._reject(message, key, now, UNKNOWN_ACCOUNT)
        if debtor.bic != message.debtor.bic or creditor.bic != message.creditor.bic:
            return self._reject(message, key, now, WRONG_BANK_IDENTIFIER)

        request = {
            "idempotency_key": key,
            "debtor_id": debtor.id,
            "creditor_id": creditor.id,
            "amount_cents": message.amount_cents,
            "reference": message.remittance,
            "end_to_end_id": message.end_to_end_id,
            "uetr": message.uetr,
        }

        try:
            completed = self._ledger.lookup_transfer(**request)
        except IdempotencyConflict:
            return self._reject(message, key, now, DUPLICATE)
        if completed is not None:
            return self._accept(message, key, now, completed)

        if now - message.accepted_at > self._timeout:
            return self._reject(message, key, now, TIMEOUT)

        try:
            transfer = self._ledger.transfer(**request)
        except InsufficientFunds:
            return self._reject(message, key, now, NOT_SPECIFIED)
        except IdempotencyConflict:
            return self._reject(message, key, now, DUPLICATE)
        return self._accept(message, key, now, transfer)

    # -- replies -----------------------------------------------------------

    def _accept(
        self,
        message: pacs008.CreditTransfer,
        key: str,
        now: datetime,
        transfer: TransferResult,
    ) -> ProcessingResult:
        report = self._report(message, key, now, reason_code=None)
        return ProcessingResult(report, pacs002.build(report), transfer)

    def _reject(
        self, message: pacs008.CreditTransfer, key: str, now: datetime, reason_code: str
    ) -> ProcessingResult:
        report = self._report(message, key, now, reason_code=reason_code)
        # Fail closed: if the audit write fails, the exception propagates and no reply is sent.
        # No money has moved at this point, so the originator can safely retry.
        self._audit.record_rejection(message, report, now)
        return ProcessingResult(report, pacs002.build(report), None)

    @staticmethod
    def _report(
        message: pacs008.CreditTransfer, key: str, now: datetime, *, reason_code: str | None
    ) -> pacs002.StatusReport:
        # The outcome is part of the id seed: the same outcome for the same payment always gets
        # the same ids, while a later different outcome (rejected, then accepted) does not reuse them.
        seed = f"{key}|{reason_code or pacs002.ACCEPTED}"
        return pacs002.report_for(
            message,
            msg_id=_derived_id("RPT", seed),
            status_id=_derived_id("STS", seed),
            created_at=now,
            reason_code=reason_code,
        )
