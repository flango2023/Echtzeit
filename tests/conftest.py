import pytest
from helpers import BANK_A, Clock, Customer

from echtzeit.audit import AuditLog
from echtzeit.db import make_engine, make_session_factory
from echtzeit.iso20022.identifiers import make_german_iban
from echtzeit.ledger import Ledger
from echtzeit.models import SYSTEM, init_db
from echtzeit.payments import InstantPaymentProcessor


@pytest.fixture
def engine(tmp_path):
    # A file database, not :memory:, so every thread sees the same data.
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine):
    return make_session_factory(engine)


@pytest.fixture
def ledger(sessions):
    return Ledger(sessions)


@pytest.fixture
def bank(ledger):
    """The system account that is the counterpart of every deposit of fictional money."""
    return ledger.create_account("Echtzeit Funding", kind=SYSTEM)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def audit(sessions):
    return AuditLog(sessions)


@pytest.fixture
def processor(ledger, audit, clock):
    return InstantPaymentProcessor(ledger, audit=audit, clock=clock)


@pytest.fixture
def customer(ledger, bank):
    counter = iter(range(1, 1000))

    def _make(name: str, bic: str = BANK_A, funds: int = 0) -> Customer:
        iban = make_german_iban(str(next(counter)))
        account_id = ledger.create_account(name, iban=iban, bic=bic)
        if funds:
            ledger.transfer(
                idempotency_key=f"fund-{account_id}",
                debtor_id=bank,
                creditor_id=account_id,
                amount_cents=funds,
            )
        return Customer(account_id, name, iban, bic)

    return _make


@pytest.fixture
def fund(ledger, bank):
    def _fund(account_id: str, cents: int, key: str | None = None):
        return ledger.transfer(
            idempotency_key=key or f"fund-{account_id}-{cents}",
            debtor_id=bank,
            creditor_id=account_id,
            amount_cents=cents,
            reference="initial funding",
        )

    return _fund
