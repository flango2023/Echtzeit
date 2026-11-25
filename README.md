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
- ISO 20022 messages for SEPA Instant: `pacs.008.001.08` (credit transfer) and
  `pacs.002.001.10` (accepted or rejected status report). Build, parse and rule checks,
  following the EPC SCT Inst Inter-PSP Implementation Guidelines 2025 v1.0 (EPC122-16).
  Parsing uses `defusedxml`, so entity-expansion and external-entity input is refused.
- Generated IBANs and BICs with valid check digits. They belong to no real bank.

ISO 20022 coverage and limits:

- One credit transfer per message, EUR only, IBAN and BIC identification, unstructured
  remittance text. Postal addresses, party identifiers, structured remittance, purpose
  codes and intermediary agents are not implemented. The EPC character-set rule is not
  enforced.
- `pacs.002` accepts only the rejection reason codes the guidelines list for a message to
  the originator PSP. The list has no "suspected fraud" code. Mapping a fraud freeze to a
  generic code is this project's modelling choice, not an EPC rule.
- The messages are not certified and are not exchanged with any real scheme.
- The ISO XSD files are not stored in this repository. Four tests validate the output
  against them and are skipped when the files are missing. To run them, download
  `pacs.008.001.08.xsd` and `pacs.002.001.10.xsd` from the ISO 20022 message definitions
  (https://www.iso20022.org), put them in a folder and run
  `ECHTZEIT_XSD_DIR=/path/to/folder pytest`. CI does not run these four tests.

Limits:

- The database is SQLite, which allows one writer at a time. The concurrency tests show
  the ledger rules hold when requests overlap. They do not show how the code behaves
  with PostgreSQL row locks.
- Only completed transfers are stored. A rejected transfer leaves no record.

Not implemented yet: processing a `pacs.008` into a ledger transfer, scam scenarios, fraud
scoring, liability report.

## Run the tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Security

Checked so far:

- XML input is parsed with `defusedxml`. Tests confirm that an entity-expansion payload and an
  external-entity payload are refused.
- `bandit` reports no issues on `src/`. Its only finding, the `xml.etree` import, is marked
  in the code because those modules only build documents and never parse input.
- `pip-audit` found no known vulnerabilities in the installed dependencies on the day of the
  check. This is a point-in-time result.
- No credentials, tokens or real personal data are stored in the repository.

Not done yet:

- No authentication, authorization or rate limiting, because there is no API yet.
- No encryption of the database, and no threat model document.
- Message-level protection such as signing is not implemented.

Do not expose this code to a network or use it with real money or real customer data.

## Ethics

- All accounts, names, IBANs, BICs and amounts are fictional. IBANs and BICs have valid check
  digits or format but belong to no real bank. Nobody has verified that no real institution
  uses the placeholder values.
- The planned scam scenarios will be synthetic. Results from synthetic data say nothing about
  how well a method would detect real fraud, and the project will not claim they do.
- The scenarios will describe fraud patterns at the level already published by regulators and
  industry. They will not include tooling that helps evade detection.

## Sources

This project is original code. These documents were read to implement the rules, and no text
or code from them is copied into the repository:

- EPC, *SEPA Instant Credit Transfer Inter-PSP Implementation Guidelines 2025 Version 1.0*
  (EPC122-16), for message elements, mandatory fields and rejection reason codes.
- ISO 20022 message definitions `pacs.008.001.08` and `pacs.002.001.10`, for element order and
  data types. The XSD files are not redistributed here.
- Regulation (EU) 2024/886 (Instant Payments Regulation), for the 10-second and
  verification-of-payee context.

For local schema validation the XSD files were downloaded from public GitHub repositories
(`sebastienrousseau/pacs008` and `prog-nov/iso20022-messages-for-go`). Only the schema files
were used, for checking output. No source code from those projects is used. Download the official
files from iso20022.org to reproduce the checks.

## License

Apache License 2.0. See `LICENSE`.
