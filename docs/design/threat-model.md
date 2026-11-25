# Threat model

Status: version 0.1, November 2026. It covers the HTTP API (`echtzeit.api`), the key file, the
database and payment-message input. All money and data in Echtzeit are fictional. This model
describes what would matter if the same design held real data, and states which mitigations
exist in the code today.

## What is protected

| Asset | Why it matters |
|---|---|
| Ledger integrity | Money must not appear, disappear or move twice. |
| Holds | A hold stops someone's money. A wrong hold harms an innocent customer; a missing one lets stolen money leave. |
| Personal data | IBANs, names and amounts in accounts, traces and the audit log. |
| API keys | A key acts as a bank, an auditor or an admin. |
| Availability | Instant payments have a 10-second limit. |

## Who interacts

- **Banks (role `psp`)**, one key per bank, bound to one BIC. A bank may be careless or
  compromised, so the API never trusts it beyond its own customers.
- **Auditor**: reads everything and changes nothing.
- **Admin**: creates accounts and issues fictional money.
- **Unauthenticated network clients**: only the health check is open to them.
- **Local operating-system users and database administrators**: they are outside the API's
  control.

Trust boundaries: the network to the API, the API to the database, and the key file on disk.

## Threats and mitigations

"Test" names the test in `tests/` that shows the mitigation works. "Not mitigated" means
nothing in the code addresses the threat yet.

### Spoofing

| Threat | Mitigation | Evidence |
|---|---|---|
| Guessing or brute-forcing a key | Keys are 32 random bytes from `secrets`. Failed attempts are rate-limited per source address. | `test_repeated_failed_logins_are_slowed_down` |
| A stolen key | Not mitigated: keys never expire, cannot be rotated without editing the key file and restarting, and there is no TLS in the code. | – |
| A bank sends a payment as another bank | The debtor agent BIC in the `pacs.008` must be the caller's BIC. | `test_a_bank_cannot_send_for_another_bank` |
| A message names the wrong bank for an account | The processor rejects a BIC that does not match the account (`RC01`). | `test_wrong_bic_is_rejected_with_rc01` |

### Tampering

| Threat | Mitigation | Evidence |
|---|---|---|
| Editing or deleting ledger rows, holds or the audit log | Database triggers make those tables append-only. | `test_ledger_entries_and_transfers_are_append_only`, `test_holds_and_releases_are_append_only`, `test_audit_rows_cannot_be_changed_or_deleted` |
| A database administrator drops the triggers | Not mitigated: there is no tamper-evident chaining. `verify_invariants` finds balances that disagree with the ledger, but not rewritten history that is consistent. | `test_verify_invariants_detects_a_tampered_balance` (partial) |
| A bank holds or releases money on another bank's account | Holds, releases and freezes act only on the caller's own accounts. | `test_holds_are_placed_and_released_by_the_account_bank_only`, `test_each_bank_freezes_only_its_own_accounts` |
| Setting fields the client should not control (`placed_by`, account kind) | Request bodies refuse unknown fields. `placed_by` comes from the key. | `test_who_placed_a_hold_cannot_be_spoofed`, `test_invalid_input_is_refused_without_echoing_it` |
| Malicious XML (entity expansion, external entities) | Parsing uses `defusedxml`. Bodies are limited to 64 KB, with or without a length header. | `test_pacs008_parse_blocks_entity_expansion_and_external_entities`, `test_large_bodies_are_refused_with_or_without_a_length` |
| Replaying a payment | Idempotency key and unique UETR. | `test_the_same_message_twice_moves_money_once`, `test_a_message_with_a_used_uetr_is_rejected_as_duplicate` |

### Repudiation

| Threat | Mitigation | Evidence |
|---|---|---|
| Denying a hold or a release | The client id from the key is stored with every hold and release. | `test_holds_are_placed_and_released_by_the_account_bank_only` |
| Denying a refused payment | Every rejection is in the audit log, and no reply is sent if the write fails. | `test_a_failed_audit_write_stops_the_reply` |
| Denying other API calls (reads, traces, deposits) | Not mitigated: there is no per-client request log. Responses carry a request id, but it is not stored. | – |

### Information disclosure

| Threat | Mitigation | Evidence |
|---|---|---|
| A bank probing which IBANs other banks hold | Another bank's account is answered exactly like a missing one. | `test_a_bank_sees_its_own_accounts_and_not_others` |
| The same probing through Verification of Payee | By design of the EPC scheme, VoP tells whether an IBAN can be verified and returns the holder name on a close match. Only rate limiting reduces scraping. | `test_vop_and_risk_assessment` |
| Traces exposing other banks' customers | A bank sees its own accounts' IBANs; other accounts appear only as BIC and amounts. Only payments the bank took part in can be traced. | `test_trace_shows_other_banks_only_as_amounts` |
| The audit log exposing other banks' rejections | A bank sees only rejections in which it is debtor or creditor agent. | `test_banks_see_only_rejections_that_involve_them` |
| Error messages leaking internals or echoing input | Unexpected errors return a generic 500. Validation errors name the field, not the value. | `test_unexpected_errors_do_not_leak_details`, `test_invalid_input_is_refused_without_echoing_it` |
| The key file read by another local user | Keys are stored only as hashes. The server refuses to start if the file is readable by group or others. New files are created with mode 600. | `test_new_keys_are_stored_only_as_hashes_in_a_private_file`, `test_unsafe_or_invalid_key_files_are_refused` |
| Responses cached by intermediaries | `Cache-Control: no-store` on every response. | `test_health_needs_no_key_and_carries_safety_headers` |
| Personal data at rest | Not mitigated: the database is not encrypted, and there is no retention period. | – |
| Data in transit | Not mitigated in the code: there is no TLS. The README says to bind to 127.0.0.1 only. | – |

### Denial of service

| Threat | Mitigation | Evidence |
|---|---|---|
| Flooding with requests | A token bucket per client and a body size limit. | `test_each_client_is_rate_limited` |
| Limits bypassed with several server processes | Not mitigated: the rate limiter is in memory and covers one process only. | – |
| Expensive traces | The depth is capped at 10. Not mitigated: each trace still replays every transfer since the payment, so its cost grows with the ledger. | – |
| One slow writer blocking others | Not mitigated: SQLite allows one writer at a time. | – |

### Elevation of privilege

| Threat | Mitigation | Evidence |
|---|---|---|
| A bank, auditor or admin acting outside its role | Every endpoint checks the role, and banks are further limited to their BIC. | `test_only_admin_creates_and_funds_accounts`, `test_a_bank_cannot_send_for_another_bank`, `test_banks_see_only_rejections_that_involve_them` |
| An admin issuing money freely | Accepted for a simulator. A real system would need dual control and limits. | – |

## Supply chain

`pip-audit` and `bandit` ran clean on 25 November 2026, and CI runs both on every push.
Dependencies have lower bounds but no lock file, so a fresh install may pull newer versions
than the ones checked.

## Out of scope

Real money and real customer data, TLS termination, key rotation and expiry, multi-instance
deployment, network-level protection and the scheme operator's infrastructure.
