# Threat model

Status: version 0.1, November 2026. It covers the HTTP API (`echtzeit.api`), the key files, the
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

- **Banks (role `psp`)**, each client bound to one BIC (a bank may hold several keys). A bank may be careless or
  compromised, so the API never trusts it beyond its own customers.
- **Auditor**: reads everything and changes nothing.
- **Admin**: creates accounts and issues fictional money.
- **Unauthenticated network clients**: only the health check is open to them.
- **Local operating-system users and database administrators**: they are outside the API's
  control.

Trust boundaries: the network to the API, the API to the database, and the key files on disk.

## Threats and mitigations

"Test" names the test in `tests/` that shows the mitigation works. "Not mitigated" means
nothing in the code addresses the threat yet.

### Spoofing

| Threat | Mitigation | Evidence |
|---|---|---|
| Guessing or brute-forcing a key | Keys are 32 random bytes from `secrets`. Failed attempts are rate-limited per source address. | `test_repeated_failed_logins_are_slowed_down` |
| A stolen key | Keys expire (90 days by default, at most 365). A client can hold a second key while rotating. A revoked key stops working without a restart, because the server re-reads the key file when it changes. A key file that becomes invalid or unreadable refuses every key: any error during a reload, not only a malformed file, empties the key list. Keys travel only over TLS or on the local machine (see "Data in transit"). | `test_keys_expire`, `test_rotation_and_revocation_work_without_a_restart`, `test_a_broken_key_file_refuses_every_key_until_fixed`, `test_a_key_file_that_cannot_be_read_refuses_every_key` |
| A bank sends a payment as another bank | The debtor agent BIC in the `pacs.008` must be the caller's BIC. | `test_a_bank_cannot_send_for_another_bank` |
| A message names the wrong bank for an account | The processor rejects a BIC that does not match the account (`RC01`). | `test_wrong_bic_is_rejected_with_rc01` |

### Tampering

| Threat | Mitigation | Evidence |
|---|---|---|
| Editing or deleting ledger rows, holds or the audit log | Database triggers make those tables append-only. | `test_ledger_entries_and_transfers_are_append_only`, `test_holds_and_releases_are_append_only`, `test_audit_rows_cannot_be_changed_or_deleted` |
| Deleting recent log rows under the cover of retention | Only `rejected_payments` and `api_requests` can be purged. A trigger refuses to delete any row younger than the table's retention period, and every delete when no period is set. A period is at least 30 days, checked by the code and by the database. Each purge leaves an append-only record in `retention_runs`. | `test_without_a_policy_nothing_is_deleted_even_by_hand`, `test_the_trigger_refuses_recent_rows_even_with_a_policy`, `test_logs_stay_unchangeable_and_the_money_record_is_never_purged`, `test_policies_below_the_minimum_or_for_other_tables_are_refused` |
| A database administrator drops the triggers | Not mitigated: there is no tamper-evident chaining. `verify_invariants` finds balances that disagree with the ledger, but not rewritten history that is consistent. | `test_verify_invariants_detects_a_tampered_balance` (partial) |
| A bank holds or releases money on another bank's account | Holds, releases and freezes act only on the caller's own accounts. | `test_holds_are_placed_and_released_by_the_account_bank_only`, `test_each_bank_freezes_only_its_own_accounts` |
| Setting fields the client should not control (`placed_by`, account kind) | Request bodies refuse unknown fields. `placed_by` comes from the key. | `test_who_placed_a_hold_cannot_be_spoofed`, `test_invalid_input_is_refused_without_echoing_it` |
| Malicious XML (entity expansion, external entities) | Parsing uses `defusedxml`. Bodies are limited to 64 KB, with or without a length header. | `test_pacs008_parse_blocks_entity_expansion_and_external_entities`, `test_large_bodies_are_refused_with_or_without_a_length` |
| Replaying a payment | Idempotency key and unique UETR. The request hash behind the key is computed over a JSON list, so two different requests cannot share it by moving a separator between fields. | `test_the_same_message_twice_moves_money_once`, `test_a_message_with_a_used_uetr_is_rejected_as_duplicate`, `test_different_requests_never_share_an_idempotency_hash` |

### Repudiation

| Threat | Mitigation | Evidence |
|---|---|---|
| Denying a hold or a release | The client id from the key is stored with every hold (`placed_by`) and release (`released_by`). | `test_holds_are_placed_and_released_by_the_account_bank_only` |
| Denying a refused payment | Every rejection is in the audit log, and no reply is sent if the write fails. | `test_a_failed_audit_write_stops_the_reply` |
| Denying other API calls (reads, traces, deposits) | Every call except the health check is written to an append-only request log: time, client, key id, method, route template, status, duration and request id. Only auditors read it. The log is written after the response, so a failed write is reported in the server log but does not change the response. | `test_every_call_is_logged_without_bodies_keys_or_path_values`, `test_the_request_log_is_for_auditors_and_append_only`, `test_a_failing_request_log_does_not_change_the_response` |

### Information disclosure

| Threat | Mitigation | Evidence |
|---|---|---|
| A bank probing which IBANs other banks hold | Another bank's account is answered exactly like a missing one. | `test_a_bank_sees_its_own_accounts_and_not_others` |
| The same probing through Verification of Payee | By design of the EPC scheme, VoP tells whether an IBAN can be verified and returns the holder name on a close match. Only rate limiting reduces scraping. | `test_vop_and_risk_assessment` |
| Traces exposing other banks' customers | A bank sees its own accounts' IBANs; other accounts appear only as BIC and amounts. Only payments the bank took part in can be traced. A freeze by a bank with no account on the payment's path gets the same 404 as a missing payment. | `test_trace_shows_other_banks_only_as_amounts`, `test_each_bank_freezes_only_its_own_accounts` |
| The audit log exposing other banks' rejections | A bank sees the rejections it sent, and those addressed to an account it really holds. A sender naming another bank and an unknown IBAN cannot place a row, or its customer's IBAN, in that bank's view. | `test_banks_see_only_rejections_that_involve_them` |
| Error messages leaking internals or echoing input | Unexpected errors return a generic 500, which carries the same safety headers and request id as every other response and is written to the request log. Validation errors name the field, not the value. | `test_unexpected_errors_do_not_leak_details`, `test_unexpected_errors_carry_the_safety_headers_and_are_logged`, `test_invalid_input_is_refused_without_echoing_it` |
| The API schema read without a key | The OpenAPI schema and the interactive documentation pages are not served. | `test_the_api_schema_is_not_served` |
| The key file read by another local user | Keys are stored only as hashes. The server refuses to start if the file is readable by group or others. Files are written with mode 600 and replaced atomically (a rename, not tested). A file opened to others while the server runs refuses every key at the next reload. | `test_new_keys_are_stored_only_as_hashes_in_a_private_file`, `test_unsafe_or_invalid_key_files_are_refused`, `test_a_key_file_opened_to_others_while_running_refuses_every_key` |
| Responses cached by intermediaries | `Cache-Control: no-store` on every response. | `test_health_needs_no_key_and_carries_safety_headers` |
| Personal data in a copied database file (backup, lost disk) | With a key file, the database is opened through SQLCipher 4 (`sqlcipher3`), which encrypts every page with AES-256 and checks an HMAC-SHA512 on each page when it is read, including pages in the write-ahead log ([SQLCipher design](https://www.zetetic.net/sqlcipher/design/)). The key is a 256-bit raw key, so no password derivation is involved. A wrong key is refused at connect time. Temporary data is kept in memory, because SQLCipher's design notes assume file-based temporary storage is switched off; the code sets this, and the `sqlcipher3` 0.6.5 build used here already defaults to it. The launcher refuses a non-loopback address without a database key. Not covered: data in the memory of the running process, exports an operator makes, and databases opened without a key, which stay plain SQLite. | `test_the_ledger_works_on_an_encrypted_file_and_nothing_is_readable_on_disk`, `test_a_wrong_key_or_no_key_cannot_open_an_encrypted_file`, `test_the_launcher_refuses_unsafe_settings` |
| The database key read by another local user or leaked | The key lives only in a file, never in the URL, an environment value or a log. The file must have mode 600, checked by the API, the launcher and the retention tool; new key files are created with mode 600 and never overwrite an existing one. Not mitigated: there is no key rotation (SQLCipher's `PRAGMA rekey` is not wired in), and losing the key file loses the database. | `test_a_new_key_file_is_private_random_and_never_overwritten`, `test_key_files_that_are_open_missing_or_malformed_are_refused`, `test_only_hex_keys_reach_the_pragma` |
| Personal data kept longer than needed | The GDPR's storage-limitation principle ([Regulation (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj), art. 5(1)(e)) asks that personal data be kept identifiable no longer than its purpose requires. An operator sets a period per log table and runs `python -m echtzeit.retention purge`. There are no default periods, because the right one depends on the purpose and on legal duties; without a period nothing is deleted. Not mitigated: purges do not run on a schedule, and the ledger, transfers and holds, which also contain IBANs and names, are never purged. | `test_a_purge_deletes_only_rows_older_than_the_period`, `test_rejected_payments_follow_their_own_policy` |
| Data in transit | The launcher (`python -m echtzeit.api.serve`) refuses to listen on a non-loopback address without a TLS certificate and key, and refuses a key file others can read or a certificate and key that do not load as a pair. Over HTTPS every response carries `Strict-Transport-Security`. The minimum protocol version (TLS 1.2) comes from Python's default server context: on 25 November 2026, with Python 3.14 and OpenSSL 3.6.4, the context uvicorn builds had `minimum_version` TLSv1_2. The code does not set it. | `test_the_launcher_refuses_unsafe_settings`, `test_hsts_is_sent_only_over_https`; HTTPS checked by hand with a self-signed certificate |
| The request log exposing personal data | The log stores route templates such as `/v1/accounts/{iban}`, not the IBAN. It never stores bodies or keys. The source address is kept only for failed authentication. The launcher turns uvicorn's access log off, because its lines would contain the full path. | `test_every_call_is_logged_without_bodies_keys_or_path_values`, `test_the_launcher_turns_the_access_log_off` |

### Denial of service

| Threat | Mitigation | Evidence |
|---|---|---|
| Flooding with requests | A token bucket per client and a body size limit, also for chunked bodies. Failed-login buckets are bounded at 10,000 sources. | `test_each_client_is_rate_limited`, `test_large_bodies_are_refused_with_or_without_a_length`, `test_failed_login_buckets_stay_bounded` |
| Amounts that are expensive to parse | An amount must be ASCII digits with an optional point and at most two fraction digits, so a short value such as `1E999990` never reaches the decimal conversion, which took 15.5 s for that value before the fix. | `test_pacs008_parse_accepts_only_plain_decimal_amounts` |
| Limits bypassed with several server processes | Not mitigated: the rate limiter is in memory and covers one process only. | – |
| Expensive traces | The depth is capped at 10. Not mitigated: each trace still replays every transfer since the payment, so its cost grows with the ledger. | – |
| One slow writer blocking others | Not mitigated: SQLite allows one writer at a time, and the request log adds one write per call. | – |

### Elevation of privilege

| Threat | Mitigation | Evidence |
|---|---|---|
| A bank, auditor or admin acting outside its role | Every endpoint checks the role, and banks are further limited to their BIC. The tests cover the routes named here and others incidentally; there is no full route-by-role matrix test. | `test_only_admin_creates_and_funds_accounts`, `test_a_bank_cannot_send_for_another_bank`, `test_banks_see_only_rejections_that_involve_them` |
| An admin issuing money freely | Accepted for a simulator. A real system would need dual control and limits. | – |

## Supply chain

`pip-audit` and `bandit` ran clean on 25 November 2026, and again on 26 November 2026 after
`sqlcipher3` was added; CI runs both on every push.
Dependencies have lower bounds but no lock file, so a fresh install may pull newer versions
than the ones checked.

## Out of scope

Real money and real customer data, certificate management, multi-instance deployment,
network-level protection and the scheme operator's infrastructure.
