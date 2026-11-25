import pytest

from echtzeit.db import make_engine, make_session_factory
from echtzeit.ledger import Ledger
from echtzeit.models import SYSTEM, init_db


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
