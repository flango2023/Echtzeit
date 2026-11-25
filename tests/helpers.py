"""Shared test helpers for payment processing tests."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from echtzeit.iso20022 import pacs008
from echtzeit.iso20022.identifiers import new_uetr

T0 = datetime(2026, 11, 25, 10, 0, 0, 0, tzinfo=UTC)
BANK_A = "ECHTDEFF"
BANK_B = "ECHTDEF1"


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    iban: str
    bic: str


def message(debtor: Customer, creditor: Customer, **overrides) -> pacs008.CreditTransfer:
    fields = {
        "msg_id": "MSG-1",
        "created_at": T0,
        "end_to_end_id": "E2E-1",
        "tx_id": "TX-1",
        "uetr": new_uetr(),
        "amount_cents": 1_000,
        "settlement_date": date(2026, 11, 25),
        "accepted_at": T0,
        "debtor": pacs008.Party(debtor.name, debtor.iban, debtor.bic),
        "creditor": pacs008.Party(creditor.name, creditor.iban, creditor.bic),
        "remittance": "Invoice 42",
    }
    fields.update(overrides)
    return pacs008.CreditTransfer(**fields)
