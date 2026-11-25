"""Append-only record of rejected payment messages.

Accepted payments are already recorded by the ledger (transfers and ledger entries). This
module records the refusals, which the ledger never sees.

Privacy: rows hold IBANs and amounts. Here they are fictional. A real deployment would need
a retention period and access control for this table, and neither is implemented.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from echtzeit.iso20022 import pacs002, pacs008
from echtzeit.models import RejectedPayment


@dataclass(frozen=True)
class RejectionRecord:
    recorded_at: datetime
    msg_id: str
    tx_id: str
    uetr: str
    end_to_end_id: str
    debtor_bic: str
    debtor_iban: str
    creditor_bic: str
    creditor_iban: str
    amount_cents: int
    reason_code: str
    reply_msg_id: str


class AuditLog:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    def record_rejection(
        self, message: pacs008.CreditTransfer, report: pacs002.StatusReport, recorded_at: datetime
    ) -> None:
        """Store one refusal. Raises if the report is not a rejection."""
        if report.status != pacs002.REJECTED or report.reason_code is None:
            raise ValueError("only rejected reports can be recorded")
        with self._sessions.begin() as session:
            session.add(
                RejectedPayment(
                    recorded_at=recorded_at,
                    msg_id=message.msg_id,
                    tx_id=message.tx_id,
                    uetr=message.uetr,
                    end_to_end_id=message.end_to_end_id,
                    debtor_bic=message.debtor.bic,
                    debtor_iban=message.debtor.iban,
                    creditor_bic=message.creditor.bic,
                    creditor_iban=message.creditor.iban,
                    amount_cents=message.amount_cents,
                    reason_code=report.reason_code,
                    reply_msg_id=report.msg_id,
                )
            )

    def rejections(self, *, reason_code: str | None = None) -> list[RejectionRecord]:
        """All recorded refusals in the order they were written, optionally for one reason."""
        statement = select(RejectedPayment).order_by(RejectedPayment.id)
        if reason_code is not None:
            statement = statement.where(RejectedPayment.reason_code == reason_code)
        with self._sessions() as session, session.begin():
            return [
                RejectionRecord(
                    recorded_at=row.recorded_at,
                    msg_id=row.msg_id,
                    tx_id=row.tx_id,
                    uetr=row.uetr,
                    end_to_end_id=row.end_to_end_id,
                    debtor_bic=row.debtor_bic,
                    debtor_iban=row.debtor_iban,
                    creditor_bic=row.creditor_bic,
                    creditor_iban=row.creditor_iban,
                    amount_cents=row.amount_cents,
                    reason_code=row.reason_code,
                    reply_msg_id=row.reply_msg_id,
                )
                for row in session.scalars(statement)
            ]

    def count_by_reason(self) -> dict[str, int]:
        return dict(Counter(record.reason_code for record in self.rejections()))
