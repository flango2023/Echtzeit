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
- Payment processing (`echtzeit.payments`): a `pacs.008` becomes a ledger transfer and the
  reply is a `pacs.002`. Accounts carry an IBAN and a BIC, and the transfer stores the
  message's UETR and end-to-end id for tracing. Checks, in order, with the reply code:
  unknown IBAN `AC01`, BIC does not match the account `RC01`, same key with different
  content `AM05`, message older than 10 seconds `AB05`, insufficient funds `MS03`.
  A repeated message is answered as accepted and moves money once. This holds even when the
  retry arrives after the 10-second limit, so a completed payment is never reported as
  timed out. The idempotency key is the originator BIC plus the `TxId`. A UETR identifies one
  payment, so a message that reuses a UETR under another `TxId` is rejected as `AM05`.

- Audit log of rejections (`echtzeit.audit`): every refused payment message is written to an
  append-only table (database triggers block updates and deletes) before the reply is sent.
  One row per attempt, with the IBANs as written in the message, so probing of unknown
  accounts stays visible. Accepted payments are recorded by the ledger itself. If the audit
  write fails, no reply is sent. No money has moved at that point, so the originator can
  retry.

Processing limits:

- The `MS03` answer for insufficient funds is this project's choice. The EPC lists no
  inter-PSP code for it, because a real originator PSP refuses such a payment before it
  creates a `pacs.008`.
- The processor does not compare the creditor name with the account holder name. That check
  (Verification of Payee) runs on the sending side before the message is built, in
  `echtzeit.vop`, as it does in the EPC scheme.
- The 10-second limit is measured from the message's acceptance time against the processor's
  clock. There is no model of network delay or clock skew.
- The audit log has no retention period, access control or tamper-evident chaining. A
  database administrator can drop the triggers. Rows hold IBANs and amounts, which would be
  personal data in a real deployment.
- The database schema changed (IBAN, BIC, UETR columns, audit table, holds). There are no migrations, so a
  database file created by an earlier version must be deleted.

- Holds (`Ledger.place_hold`, `freeze_available`, `release_hold`): money stopped on an account
  without moving it. Payments can spend only the balance minus active holds. Holds and
  releases are append-only rows. `freeze_available` records both the requested and the
  secured amount.
- Tracing (`echtzeit.tracing`): follows a payment through later transfers, account by
  account, and reports how much is still on each account and how much moved on.
  `freeze_traced_funds` traces and holds in one transaction. Mixed money is attributed by one
  of two rules that give an upper and a lower bound. Design and limits:
  [docs/design/holds-and-tracing.md](docs/design/holds-and-tracing.md).

Holds and tracing limits:

- The two attribution rules are bounds for an estimate, not legal tracing rules.
- Who may place or release a hold is not enforced. `placed_by` is a free-text label.
- Held money is only stopped. Returning it to the victim is not implemented.
- In the scenarios, the time until a freeze is the victim's reporting delay, which is an
  assumption. The time a bank needs to act on a report is not modelled.

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
- Rejected payment messages are kept in the audit log. A direct ledger call that fails (for
  example in a test) leaves no record.

Scam scenarios, payee check and risk score:

- Verification of Payee (`echtzeit.vop`): the four EPC outcomes (match, close match, no
  match, not possible). The name comparison method and its threshold are this project's
  choice, because the EPC leaves matching to each PSP.
- Risk score (`echtzeit.risk`): transparent rules (new payee, amount spike, balance drain,
  payee-name result, never-paid receiver, money passing straight through). Each result lists
  the rules that fired. Points and thresholds are uncalibrated choices in `RiskConfig`.
- Simulation (`echtzeit.simulation`): a seeded synthetic population with normal payments and
  three scam patterns paraphrased from the EPC 2025 fraud trends report (safe account, invoice
  with a changed IBAN, one or two levels of money mules). Every payment goes through the
  payee check, the risk score, a `pacs.008` message and the ledger. The victim's report
  triggers a trace and a freeze. Three policies are compared on the same world: no scoring,
  sending-side scoring, and sending-side scoring plus a receiving-side freeze.
- Results: [docs/results/scenarios-seed-7.md](docs/results/scenarios-seed-7.md), generated by
  `python -m echtzeit.scenario_report --seed 7 --spread-seeds 10 --out docs/results/scenarios-seed-7.md`
  (about 80 seconds). It includes a spread over seeds 1 to 10. Design, sources and
  assumptions: [docs/design/scenarios-and-scoring.md](docs/design/scenarios-and-scoring.md).

Scenario limits:

- The same author wrote the scenarios and the rules, so the results are circular by
  construction. They show how the rules behave against stated assumptions, not how well
  they would detect real fraud.
- Every probability, delay and amount in the scenarios is an assumption and is printed in
  the results file.
- Assessment times are from one machine with SQLite and exclude network and messaging.

Not implemented yet: a liability report that compares regimes. Planned regimes for version
1: UK, EU (marked provisional, because the PSR text is a provisional agreement) and a
no-refund baseline. Brazil's MED is planned for later, after its rules are read from the
Banco Central texts.

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
- `bandit` reports no issues on `src/`. Two kinds of finding are marked in the code with the
  reason: the `xml.etree` import (those modules only build documents and never parse input)
  and the seeded random generator in the simulation (needed for reproducible runs; payment
  identifiers come from `uuid4`).
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
- The scam scenarios are synthetic. Results from synthetic data say nothing about how well a
  method would detect real fraud, and the project does not claim they do.
- The scenarios describe fraud patterns at the level already published by the EPC, and the
  risk rules use generic, widely published signals. The project contains no tooling that
  helps evade detection.

## Sources

This project is original code. These documents were read to implement the rules, and no text
or code from them is copied into the repository:

- EPC, *SEPA Instant Credit Transfer Inter-PSP Implementation Guidelines 2025 Version 1.0*
  (EPC122-16), for message elements, mandatory fields and rejection reason codes.
- ISO 20022 message definitions `pacs.008.001.08` and `pacs.002.001.10`, for element order and
  data types. The XSD files are not redistributed here.
- Regulation (EU) 2024/886 (Instant Payments Regulation), for the 10-second and
  verification-of-payee context.
- EPC, *2025 Payments Threats and Fraud Trends Report* (EPC162-24, version 2.0), for the scam
  patterns (bank employee impersonation and safe accounts, IBAN manipulation at the invoicing
  stage, money mules with one or two levels).
- EPC FAQ *Clarifications about the provision of VOP-services for bulk files*, for the four
  Verification of Payee outcomes.
- European Parliament press release on the payment services deal (November 2025), for the
  provisional receiving-PSP freeze obligation.

For local schema validation the XSD files were downloaded from public GitHub repositories
(`sebastienrousseau/pacs008` and `prog-nov/iso20022-messages-for-go`). Only the schema files
were used, for checking output. No source code from those projects is used. Download the official
files from iso20022.org to reproduce the checks.

## License

Apache License 2.0. See `LICENSE`.
