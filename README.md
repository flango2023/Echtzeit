# Echtzeit

A scam-liability lab for instant payments. Work in progress.

All money in this project is fictional. Nothing connects to a bank, a payment
scheme, or real customer data.

## Status

Implemented:

- Fictional EUR accounts with integer-cent balances.
- Append-only double-entry ledger.
- Atomic, idempotent transfers (SQLite through SQLAlchemy).
- Tests for ledger invariants, rollback on failure and concurrent requests.

Limits:

- The database is SQLite, which allows one writer at a time. The concurrency tests show
  the ledger rules hold when requests overlap. They do not show how the code behaves
  with PostgreSQL row locks.
- Only completed transfers are stored. A rejected transfer leaves no record.

Not implemented yet: ISO 20022 messages, scam scenarios, fraud scoring, liability report.

## Run the tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

Apache License 2.0. See `LICENSE`.
