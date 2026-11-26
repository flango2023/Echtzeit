import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from echtzeit.api.app import create_app, create_app_from_env
from echtzeit.api.auth import (
    KeyFileError,
    KeyStore,
    Role,
    add_key,
    hash_key,
    list_keys,
    revoke_key,
)
from echtzeit.api.ratelimit import RateLimiter
from echtzeit.iso20022 import pacs002, pacs008
from echtzeit.iso20022.identifiers import make_german_iban, new_uetr

BANK_A, BANK_B, BANK_C, BANK_D = "ECHTDEFF", "ECHTDEF1", "ECHTDEF2", "ECHTDEF3"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
KEYS = {
    "bank-a": "ez_bank_a_test_key_0000000000000000000000000000",
    "bank-b": "ez_bank_b_test_key_0000000000000000000000000000",
    "bank-c": "ez_bank_c_test_key_0000000000000000000000000000",
    "bank-d": "ez_bank_d_test_key_0000000000000000000000000000",
    "auditor": "ez_auditor_test_key_000000000000000000000000000",
    "admin": "ez_admin_test_key_00000000000000000000000000000",
}


EXPIRES = "2027-11-25T00:00:00+00:00"


def keystore() -> KeyStore:
    entries = []
    for client_id, key in KEYS.items():
        entry = {
            "client_id": client_id,
            "key_sha256": hash_key(key),
            "key_id": client_id.replace("-", "") + "0000",
            "expires_at": EXPIRES,
        }
        if client_id.startswith("bank-"):
            entry["role"] = "psp"
            entry["bic"] = {"bank-a": BANK_A, "bank-b": BANK_B, "bank-c": BANK_C, "bank-d": BANK_D}[
                client_id
            ]
        else:
            entry["role"] = client_id
        entries.append(entry)
    return KeyStore(entries)


def auth(client_id: str) -> dict:
    return {"Authorization": f"Bearer {KEYS[client_id]}"}


@pytest.fixture
def api(ledger, audit):
    app = create_app(ledger, audit, keystore(), clock=lambda: NOW)
    return TestClient(app)


@pytest.fixture
def world(api):
    """Alice at bank A with money, Bob at bank B."""
    alice = {"holder_name": "Alice Example", "iban": make_german_iban("1"), "bic": BANK_A}
    bob = {"holder_name": "Bob Example", "iban": make_german_iban("2"), "bic": BANK_B}
    for account in (alice, bob):
        assert api.post("/v1/accounts", json=account, headers=auth("admin")).status_code == 201
    deposit = {"amount_cents": 100_000, "idempotency_key": "alice-1"}
    response = api.post(
        f"/v1/accounts/{alice['iban']}/deposits", json=deposit, headers=auth("admin")
    )
    assert response.status_code == 201
    return alice, bob


def payment(alice, bob, cents=10_000, **overrides) -> bytes:
    fields = {
        "msg_id": "MSG-1",
        "created_at": NOW,
        "end_to_end_id": "E2E-1",
        "tx_id": "TX-1",
        "uetr": new_uetr(),
        "amount_cents": cents,
        "settlement_date": date(2026, 11, 25),
        "accepted_at": NOW,
        "debtor": pacs008.Party(alice["holder_name"], alice["iban"], alice["bic"]),
        "creditor": pacs008.Party(bob["holder_name"], bob["iban"], bob["bic"]),
    }
    fields.update(overrides)
    return pacs008.build(pacs008.CreditTransfer(**fields))


def send(api, client_id, xml):
    headers = {**auth(client_id), "Content-Type": "application/xml"}
    return api.post("/v1/payments", content=xml, headers=headers)


# -- authentication -------------------------------------------------------------------------


def test_health_needs_no_key_and_carries_safety_headers(api):
    response = api.get("/v1/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {KEYS['admin']}"},
     {"Authorization": "Bearer ez_" + "x" * 300}],
)  # fmt: skip
def test_missing_or_wrong_keys_are_refused(api, headers):
    response = api.get("/v1/invariants", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"


def test_repeated_failed_logins_are_slowed_down(ledger, audit):
    slow = RateLimiter(capacity=3, refill_per_second=0.001, clock=lambda: 0.0)
    api = TestClient(create_app(ledger, audit, keystore(), failed_auth_limiter=slow))
    codes = [
        api.get("/v1/invariants", headers={"Authorization": "Bearer ez_guess"}).status_code
        for _ in range(5)
    ]
    assert codes == [401, 401, 401, 429, 429]


def test_each_client_is_rate_limited(ledger, audit):
    tight = RateLimiter(capacity=2, refill_per_second=0.5, clock=lambda: 0.0)
    api = TestClient(create_app(ledger, audit, keystore(), limiter=tight))
    codes = [api.get("/v1/invariants", headers=auth("auditor")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = api.get("/v1/invariants", headers=auth("auditor"))
    assert limited.headers["retry-after"] == "2"  # 1.5 s to the next token, rounded up
    assert api.get("/v1/invariants", headers=auth("admin")).status_code == 200  # own bucket


# -- accounts -------------------------------------------------------------------------------


def test_only_admin_creates_and_funds_accounts(api, world):
    alice, _ = world
    account = {"holder_name": "Eve", "iban": make_german_iban("3"), "bic": BANK_A}
    assert api.post("/v1/accounts", json=account, headers=auth("bank-a")).status_code == 403
    assert api.post("/v1/accounts", json=account, headers=auth("auditor")).status_code == 403
    deposit = {"amount_cents": 1, "idempotency_key": "x"}
    assert (
        api.post(
            f"/v1/accounts/{alice['iban']}/deposits", json=deposit, headers=auth("bank-a")
        ).status_code
        == 403
    )


def test_a_deposit_is_idempotent(api, world):
    alice, _ = world
    deposit = {"amount_cents": 100_000, "idempotency_key": "alice-1"}
    again = api.post(f"/v1/accounts/{alice['iban']}/deposits", json=deposit, headers=auth("admin"))
    assert again.json()["replayed"] is True
    assert (
        api.get(f"/v1/accounts/{alice['iban']}", headers=auth("bank-a")).json()["balance_cents"]
        == 100_000
    )


def test_a_bank_sees_its_own_accounts_and_not_others(api, world):
    alice, bob = world
    own = api.get(f"/v1/accounts/{alice['iban']}", headers=auth("bank-a"))
    assert own.status_code == 200 and own.json()["available_cents"] == 100_000
    other = api.get(f"/v1/accounts/{bob['iban']}", headers=auth("bank-a"))
    missing = api.get(f"/v1/accounts/{make_german_iban('99')}", headers=auth("bank-a"))
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()  # cannot tell "exists elsewhere" from "does not exist"
    assert api.get(f"/v1/accounts/{bob['iban']}", headers=auth("auditor")).status_code == 200


def test_invalid_input_is_refused_without_echoing_it(api):
    secret = "ez_this_value_must_not_come_back"
    body = {"holder_name": secret, "iban": 12345, "bic": BANK_A}
    response = api.post("/v1/accounts", json=body, headers=auth("admin"))
    assert response.status_code == 422
    assert secret not in response.text and "12345" not in response.text
    extra = {"holder_name": "X", "iban": make_german_iban("5"), "bic": BANK_A, "kind": "SYSTEM"}
    assert api.post("/v1/accounts", json=extra, headers=auth("admin")).status_code == 422


def test_unexpected_errors_do_not_leak_details(ledger, audit, monkeypatch):
    api = TestClient(create_app(ledger, audit, keystore()), raise_server_exceptions=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(type(ledger), "verify_invariants", broken)
    response = api.get("/v1/invariants", headers=auth("auditor"))
    assert response.status_code == 500
    assert "hunter2" not in response.text and response.json()["title"] == "Internal error"


# -- payments -------------------------------------------------------------------------------


def test_a_bank_sends_a_payment_and_gets_a_pacs002(api, world):
    alice, bob = world
    response = send(api, "bank-a", payment(alice, bob))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert pacs002.parse(response.content).status == pacs002.ACCEPTED
    assert (
        api.get(f"/v1/accounts/{bob['iban']}", headers=auth("bank-b")).json()["balance_cents"]
        == 10_000
    )


def test_a_bank_cannot_send_for_another_bank(api, world):
    alice, bob = world
    assert send(api, "bank-b", payment(alice, bob)).status_code == 403
    assert send(api, "auditor", payment(alice, bob)).status_code == 403
    assert (
        api.get(f"/v1/accounts/{bob['iban']}", headers=auth("bank-b")).json()["balance_cents"] == 0
    )


def test_bad_messages_are_refused_with_the_problems_listed(api, world):
    response = send(api, "bank-a", b"<not-a-payment/>")
    assert response.status_code == 400
    assert response.json()["problems"]
    wrong_type = api.post(
        "/v1/payments", content=b"<x/>", headers={**auth("bank-a"), "Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415


def test_large_bodies_are_refused_with_or_without_a_length(ledger, audit, world):
    api = TestClient(create_app(ledger, audit, keystore(), max_body_bytes=1_000))
    headers = {**auth("bank-a"), "Content-Type": "application/xml"}
    assert api.post("/v1/payments", content=b"x" * 1_001, headers=headers).status_code == 413

    def chunks():
        for _ in range(5):
            yield b"x" * 400

    chunked = api.post("/v1/payments", content=chunks(), headers=headers)
    assert chunked.status_code == 413
    json_headers = {**auth("admin"), "Content-Type": "application/json"}
    chunked_json = api.post("/v1/accounts", content=chunks(), headers=json_headers)
    assert chunked_json.status_code == 413


def test_vop_and_risk_assessment(api, world):
    alice, bob = world
    vop = api.post(
        "/v1/vop", json={"iban": bob["iban"], "name": "Bob Exampel"}, headers=auth("bank-a")
    )
    assert vop.json() == {"outcome": "CLOSE_MATCH", "suggested_name": "Bob Example"}
    body = {
        "debtor_iban": alice["iban"],
        "creditor_iban": bob["iban"],
        "name": "Bob Example",
        "amount_cents": 90_000,
    }
    own = api.post("/v1/risk/assessments", json=body, headers=auth("bank-a"))
    assert own.status_code == 200
    assert {r["rule"] for r in own.json()["reasons"]} >= {"NEW_PAYEE", "BALANCE_DRAIN"}
    assert api.post("/v1/risk/assessments", json=body, headers=auth("bank-b")).status_code == 404


# -- holds ----------------------------------------------------------------------------------


def test_holds_are_placed_and_released_by_the_account_bank_only(api, world, engine):
    alice, _bob = world
    body = {"amount_cents": 40_000, "reason": "suspected scam"}
    assert (
        api.post(
            f"/v1/accounts/{alice['iban']}/holds", json=body, headers=auth("bank-b")
        ).status_code
        == 404
    )
    created = api.post(f"/v1/accounts/{alice['iban']}/holds", json=body, headers=auth("bank-a"))
    assert created.status_code == 201
    hold = created.json()
    assert hold["placed_by"] == "bank-a"  # taken from the key, never from the request
    assert (
        api.get(f"/v1/accounts/{alice['iban']}", headers=auth("bank-a")).json()["available_cents"]
        == 60_000
    )

    release = {"reason": "cleared"}
    assert (
        api.post(
            f"/v1/holds/{hold['id']}/release", json=release, headers=auth("bank-b")
        ).status_code
        == 404
    )
    assert (
        api.post(
            f"/v1/holds/{hold['id']}/release", json=release, headers=auth("bank-a")
        ).status_code
        == 200
    )
    assert (
        api.post(
            f"/v1/holds/{hold['id']}/release", json=release, headers=auth("bank-a")
        ).status_code
        == 409
    )
    with engine.connect() as connection:
        released_by = connection.execute(text("SELECT released_by FROM hold_releases")).all()
    assert released_by == [("bank-a",)]


def test_who_placed_a_hold_cannot_be_spoofed(api, world):
    alice, _ = world
    body = {"amount_cents": 1, "reason": "r", "placed_by": "bank-b"}
    assert (
        api.post(
            f"/v1/accounts/{alice['iban']}/holds", json=body, headers=auth("bank-a")
        ).status_code
        == 422
    )


def test_a_hold_beyond_spendable_money_is_a_conflict(api, world):
    alice, _ = world
    body = {"amount_cents": 100_001, "reason": "r"}
    assert (
        api.post(
            f"/v1/accounts/{alice['iban']}/holds", json=body, headers=auth("bank-a")
        ).status_code
        == 409
    )
    partial = api.post(
        f"/v1/accounts/{alice['iban']}/holds",
        json={**body, "mode": "available"},
        headers=auth("bank-a"),
    )
    assert partial.status_code == 201 and partial.json()["amount_cents"] == 100_000


# -- trace and freeze -----------------------------------------------------------------------


@pytest.fixture
def chain(api, world, ledger):
    """Alice (bank A) pays Bob (bank B); Bob forwards part to Carol (bank C)."""
    alice, bob = world
    carol = {"holder_name": "Carol Example", "iban": make_german_iban("4"), "bic": BANK_C}
    api.post("/v1/accounts", json=carol, headers=auth("admin"))
    uetr = new_uetr()
    assert send(api, "bank-a", payment(alice, bob, 50_000, uetr=uetr)).status_code == 200
    onward = payment(
        bob, carol, 30_000, uetr=new_uetr(), tx_id="TX-2", msg_id="MSG-2", end_to_end_id="E2E-2"
    )
    assert send(api, "bank-b", onward).status_code == 200
    return uetr, alice, bob, carol


def test_trace_shows_other_banks_only_as_amounts(api, chain):
    uetr, _alice, bob, carol = chain
    query = "?attribution=TRACED_FUNDS_FIRST"
    for_a = api.get(f"/v1/payments/{uetr}/trace{query}", headers=auth("bank-a"))
    assert for_a.status_code == 200
    accounts = {a["bic"]: a for a in for_a.json()["accounts"]}
    assert accounts[BANK_B]["remaining_cents"] == 20_000 and "iban" not in accounts[BANK_B]
    assert accounts[BANK_C]["remaining_cents"] == 30_000 and "iban" not in accounts[BANK_C]

    for_b = {
        a["bic"]: a
        for a in api.get(f"/v1/payments/{uetr}/trace{query}", headers=auth("bank-b")).json()[
            "accounts"
        ]
    }
    assert for_b[BANK_B]["iban"] == bob["iban"] and "iban" not in for_b[BANK_C]

    full = api.get(f"/v1/payments/{uetr}/trace{query}", headers=auth("auditor")).json()["accounts"]
    assert {a["iban"] for a in full} == {bob["iban"], carol["iban"]}

    assert api.get(f"/v1/payments/{uetr}/trace{query}", headers=auth("bank-c")).status_code == 404
    assert api.get(f"/v1/payments/{uetr}/trace{query}", headers=auth("admin")).status_code == 403
    assert api.get(f"/v1/payments/{uetr}/trace", headers=auth("bank-a")).status_code == 422


def test_each_bank_freezes_only_its_own_accounts(api, chain, ledger):
    uetr, _, bob, carol = chain
    body = {"attribution": "TRACED_FUNDS_FIRST", "reason": "reported scam"}
    by_a = api.post(f"/v1/payments/{uetr}/freeze", json=body, headers=auth("bank-a")).json()
    assert by_a["holds"] == [] and by_a["secured_cents"] == 0  # bank A holds none of the money
    missing = api.post(f"/v1/payments/{new_uetr()}/freeze", json=body, headers=auth("bank-d"))
    uninvolved = api.post(f"/v1/payments/{uetr}/freeze", json=body, headers=auth("bank-d"))
    assert missing.status_code == uninvolved.status_code == 404
    assert missing.json() == uninvolved.json()

    by_c = api.post(f"/v1/payments/{uetr}/freeze", json=body, headers=auth("bank-c")).json()
    assert by_c["secured_cents"] == 30_000
    assert [h["placed_by"] for h in by_c["holds"]] == ["bank-c"]
    assert (
        api.get(f"/v1/accounts/{carol['iban']}", headers=auth("bank-c")).json()["available_cents"]
        == 0
    )
    assert (
        api.get(f"/v1/accounts/{bob['iban']}", headers=auth("bank-b")).json()["available_cents"]
        == 20_000
    )
    assert api.get("/v1/invariants", headers=auth("auditor")).json() == {"ok": True, "problems": []}


# -- audit log ------------------------------------------------------------------------------


def test_banks_see_only_rejections_that_involve_them(api, world):
    alice, bob = world
    too_much = payment(alice, bob, 1_000_000)
    assert pacs002.parse(send(api, "bank-a", too_much).content).reason_code == "MS03"
    assert len(api.get("/v1/audit/rejections", headers=auth("bank-a")).json()) == 1
    assert len(api.get("/v1/audit/rejections", headers=auth("bank-b")).json()) == 1
    assert api.get("/v1/audit/rejections", headers=auth("bank-c")).json() == []
    # Bank A names bank C and an IBAN C does not hold: C must not see the row or A's IBAN.
    fake = {"holder_name": "Nobody", "iban": make_german_iban("999"), "bic": BANK_C}
    named_c = payment(alice, fake, msg_id="MSG-2", tx_id="TX-2")
    assert pacs002.parse(send(api, "bank-a", named_c).content).reason_code == "AC01"
    assert api.get("/v1/audit/rejections", headers=auth("bank-c")).json() == []
    assert len(api.get("/v1/audit/rejections", headers=auth("bank-a")).json()) == 2
    assert (
        len(api.get("/v1/audit/rejections?reason_code=MS03", headers=auth("auditor")).json()) == 1
    )
    assert api.get("/v1/audit/rejections", headers=auth("admin")).status_code == 403
    assert api.get("/v1/invariants", headers=auth("bank-a")).status_code == 403


# -- key store ------------------------------------------------------------------------------


def test_new_keys_are_stored_only_as_hashes_in_a_private_file(tmp_path):
    path = tmp_path / "keys.json"
    key, key_id = add_key(path, "bank-a", Role.PSP, BANK_A)
    assert key.startswith("ez_") and len(key) > 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert key not in path.read_text()
    store = KeyStore.from_file(path)
    found = store.authenticate(key)
    assert (found.client_id, found.key_id) == ("bank-a", key_id)
    assert store.authenticate(key + "x") is None
    second, _ = add_key(path, "auditor", Role.AUDITOR, None)
    assert KeyStore.from_file(path).authenticate(second).role is Role.AUDITOR
    listing = list_keys(path)
    assert {entry["client_id"] for entry in listing} == {"bank-a", "auditor"}
    assert all("key_sha256" not in entry for entry in listing)


def test_keys_expire(tmp_path):
    path = tmp_path / "keys.json"
    created = datetime(2026, 1, 1, tzinfo=UTC)
    key, _ = add_key(path, "bank-a", Role.PSP, BANK_A, valid_days=30, now=created)
    store = KeyStore.from_file(path)
    assert store.authenticate(key, created + timedelta(days=29)) is not None
    assert store.authenticate(key, created + timedelta(days=30)) is None
    for days in (0, 366):
        with pytest.raises(KeyFileError, match="valid_days"):
            add_key(path, "bank-b", Role.PSP, BANK_B, valid_days=days)


def test_rotation_and_revocation_work_without_a_restart(tmp_path):
    path = tmp_path / "keys.json"
    clock = {"t": 0.0}
    old, old_id = add_key(path, "bank-a", Role.PSP, BANK_A)
    store = KeyStore.from_file(path, monotonic=lambda: clock["t"])
    new, new_id = add_key(path, "bank-a", Role.PSP, BANK_A)  # rotation: both keys valid
    clock["t"] += 5
    assert store.authenticate(old).key_id == old_id
    assert store.authenticate(new).key_id == new_id
    revoke_key(path, old_id)
    clock["t"] += 5
    assert store.authenticate(old) is None
    assert store.authenticate(new).key_id == new_id
    with pytest.raises(KeyFileError, match="no key"):
        revoke_key(path, old_id)


def test_a_broken_key_file_refuses_every_key_until_fixed(tmp_path):
    path = tmp_path / "keys.json"
    clock = {"t": 0.0}
    key, _ = add_key(path, "bank-a", Role.PSP, BANK_A)
    store = KeyStore.from_file(path, monotonic=lambda: clock["t"])
    good = path.read_text()
    path.write_text("[not json")
    clock["t"] += 5
    assert store.authenticate(key) is None and store.broken
    path.write_text(good)
    clock["t"] += 5
    assert store.authenticate(key) is not None and not store.broken


def test_unsafe_or_invalid_key_files_are_refused(tmp_path):
    path = tmp_path / "keys.json"
    add_key(path, "bank-a", Role.PSP, BANK_A)
    os.chmod(path, 0o644)
    with pytest.raises(KeyFileError, match="chmod 600"):
        KeyStore.from_file(path)
    with pytest.raises(KeyFileError, match="does not exist"):
        KeyStore.from_file(tmp_path / "missing.json")
    base = {
        "client_id": "a",
        "key_id": "abcd1234",
        "key_sha256": hash_key("ez_x"),
        "expires_at": EXPIRES,
    }
    bad = [
        [{**base, "role": "psp"}],  # psp without bic
        [{**base, "role": "auditor", "bic": BANK_A}],
        [{**base, "role": "root"}],
        [{**base, "role": "admin", "key_sha256": "short"}],
        [{**base, "role": "admin", "key_id": "x"}],
        [{**base, "role": "admin", "expires_at": "2027-01-01T00:00:00"}],  # no time zone
        [{k: v for k, v in {**base, "role": "admin"}.items() if k != "expires_at"}],
        [{**base, "role": "admin"}, {**base, "role": "admin"}],  # duplicate key_id
        [
            {**base, "role": "admin"},
            {**base, "role": "auditor", "key_id": "efgh5678"},
        ],  # role differs
    ]
    for entries in bad:
        with pytest.raises(KeyFileError):
            KeyStore(entries)
    with pytest.raises(KeyFileError):
        add_key(tmp_path / "other.json", "x", Role.PSP, None)
    assert not (tmp_path / "other.json").exists()


# -- request log ----------------------------------------------------------------------------


def test_every_call_is_logged_without_bodies_keys_or_path_values(api, world):
    alice, _ = world
    response = api.get(f"/v1/accounts/{alice['iban']}", headers=auth("bank-a"))
    api.get("/v1/invariants", headers={"Authorization": "Bearer ez_wrong"})
    api.get("/v1/health")
    rows = api.get("/v1/audit/requests", headers=auth("auditor")).json()
    by_route = {(row["method"], row["route"], row["status"]): row for row in rows}
    read = by_route[("GET", "/v1/accounts/{iban}", 200)]
    assert read["client_id"] == "bank-a" and read["key_id"] == "banka0000"
    assert read["request_id"] == response.headers["x-request-id"]
    assert read["source"] is None
    failed = by_route[("GET", "/v1/invariants", 401)]
    assert failed["client_id"] is None and failed["source"]
    assert all(row["route"] != "/v1/health" for row in rows)
    text = json.dumps(rows)
    assert alice["iban"] not in text and "ez_" not in text and "Alice" not in text


def test_the_request_log_is_for_auditors_and_append_only(api, engine):
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    assert api.get("/v1/audit/requests", headers=auth("bank-a")).status_code == 403
    assert api.get("/v1/audit/requests", headers=auth("admin")).status_code == 403
    mine = api.get("/v1/audit/requests?client_id=auditor&limit=5", headers=auth("auditor")).json()
    assert all(row["client_id"] == "auditor" for row in mine)
    for statement in ("UPDATE api_requests SET status = 200", "DELETE FROM api_requests"):
        # Updates are always refused; deletes until the retention period ends (none is set).
        with (
            pytest.raises(DatabaseError, match="append-only|retention period ends"),
            engine.begin() as conn,
        ):
            conn.execute(text(statement))


def test_a_failing_request_log_does_not_change_the_response(ledger, audit, monkeypatch):
    api = TestClient(create_app(ledger, audit, keystore()))

    def broken(*_args, **_kwargs):
        raise RuntimeError("log store down")

    monkeypatch.setattr(type(audit), "record_request", broken)
    assert api.get("/v1/invariants", headers=auth("auditor")).status_code == 200


# -- transport security ---------------------------------------------------------------------


def test_hsts_is_sent_only_over_https(ledger, audit):
    app = create_app(ledger, audit, keystore())
    secure = TestClient(app, base_url="https://testserver").get("/v1/health")
    assert secure.headers["strict-transport-security"] == "max-age=31536000"
    plain = TestClient(app).get("/v1/health")
    assert "strict-transport-security" not in plain.headers


def test_the_launcher_refuses_unsafe_settings(tmp_path):
    from echtzeit.api.serve import ServeConfigError, check_config, is_loopback

    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5")
    assert not check_config("127.0.0.1", 8000, None, None).tls
    with pytest.raises(ServeConfigError, match="without TLS"):
        check_config("0.0.0.0", 8000, None, None)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to make a test certificate")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        + ["-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    os.chmod(key, 0o644)
    with pytest.raises(ServeConfigError, match="chmod 600"):
        check_config("0.0.0.0", 8443, cert, key)
    os.chmod(key, 0o600)
    with pytest.raises(ServeConfigError, match="unencrypted database"):
        check_config("0.0.0.0", 8443, cert, key)
    db_key = tmp_path / "db.key"
    db_key.write_text("ab" * 32)
    os.chmod(db_key, 0o644)
    with pytest.raises(ServeConfigError, match="chmod 600"):
        check_config("0.0.0.0", 8443, cert, key, db_key)
    os.chmod(db_key, 0o600)
    config = check_config("0.0.0.0", 8443, cert, key, db_key)
    assert config.tls and config.db_key_file == db_key
    db_key.write_text("not a key")
    with pytest.raises(ServeConfigError, match="64 hex"):
        check_config("127.0.0.1", 8000, None, None, db_key)
    with pytest.raises(ServeConfigError, match="both"):
        check_config("127.0.0.1", 8443, cert, None)
    with pytest.raises(ServeConfigError, match="does not exist"):
        check_config("0.0.0.0", 8443, tmp_path / "missing.pem", key)
    with pytest.raises(ServeConfigError, match="port"):
        check_config("127.0.0.1", 0, None, None)
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    with pytest.raises(ServeConfigError, match="do not load"):
        check_config("0.0.0.0", 8443, junk, key, db_key)


def test_the_server_does_not_start_without_a_key_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ECHTZEIT_API_KEYS_FILE", raising=False)
    with pytest.raises(RuntimeError, match="ECHTZEIT_API_KEYS_FILE"):
        create_app_from_env()
    path = tmp_path / "keys.json"
    path.write_text(json.dumps([]))
    os.chmod(path, 0o666)
    monkeypatch.setenv("ECHTZEIT_API_KEYS_FILE", str(path))
    monkeypatch.setenv("ECHTZEIT_DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    with pytest.raises(KeyFileError):
        create_app_from_env()


def test_a_key_file_that_cannot_be_read_refuses_every_key(tmp_path):
    """Any failure while reloading fails closed, including I/O and decoding errors."""
    path = tmp_path / "keys.json"
    clock = {"t": 0.0}
    key, key_id = add_key(path, "bank-a", Role.PSP, BANK_A)
    store = KeyStore.from_file(path, monotonic=lambda: clock["t"])
    assert store.authenticate(key) is not None

    revoke_key(path, key_id)  # the file changes, then becomes unreadable before the reload
    os.chmod(path, 0o000)
    try:
        clock["t"] += 5
        assert store.authenticate(key) is None and store.broken
        clock["t"] += 5
        assert store.authenticate(key) is None  # still refused, the revoked key never returns
    finally:
        os.chmod(path, 0o600)

    other = tmp_path / "other.json"
    second, _ = add_key(other, "bank-a", Role.PSP, BANK_A)
    store = KeyStore.from_file(other, monotonic=lambda: clock["t"])
    other.write_bytes(b"\xff\xfe not utf-8")
    clock["t"] += 5
    assert store.authenticate(second) is None and store.broken


def test_key_entries_with_out_of_range_times_are_refused():
    entry = {
        "client_id": "a",
        "key_id": "abcd1234",
        "key_sha256": hash_key("ez_x"),
        "role": "admin",
        "expires_at": "9999-12-31T23:59:59-05:00",
    }
    with pytest.raises(KeyFileError):
        KeyStore([entry])


def test_the_api_schema_is_not_served(api):
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert api.get(path).status_code == 404


def test_unexpected_errors_carry_the_safety_headers_and_are_logged(ledger, audit, monkeypatch):
    api = TestClient(create_app(ledger, audit, keystore()), raise_server_exceptions=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(type(ledger), "verify_invariants", broken)
    response = api.get("/v1/invariants", headers=auth("auditor"))
    assert response.status_code == 500 and "hunter2" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    request_id = response.headers["x-request-id"]
    logged = [r for r in audit.requests() if r["request_id"] == request_id]
    assert [(r["route"], r["status"]) for r in logged] == [("/v1/invariants", 500)]


def test_a_key_file_opened_to_others_while_running_refuses_every_key(tmp_path):
    path = tmp_path / "keys.json"
    clock = {"t": 0.0}
    key, _ = add_key(path, "bank-a", Role.PSP, BANK_A)
    store = KeyStore.from_file(path, monotonic=lambda: clock["t"])
    os.chmod(path, 0o644)
    clock["t"] += 5
    assert store.authenticate(key) is None and store.broken


def test_failed_login_buckets_stay_bounded():
    from echtzeit.api import ratelimit

    clock = {"t": 0.0}
    limiter = RateLimiter(capacity=2, refill_per_second=1, clock=lambda: clock["t"])
    for i in range(ratelimit.MAX_BUCKETS + 5):
        limiter.acquire(f"source-{i}")
        clock["t"] += 0.001
    assert len(limiter._buckets) <= ratelimit.MAX_BUCKETS
    clock["t"] = 0.0
    burst = RateLimiter(capacity=2, refill_per_second=0.001, clock=lambda: clock["t"])
    assert [burst.acquire("x") == 0 for _ in range(3)] == [True, True, False]


def test_the_launcher_turns_the_access_log_off(tmp_path, monkeypatch):
    import uvicorn

    from echtzeit.api import serve

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: seen.update(kwargs))
    keys = tmp_path / "keys.json"
    add_key(keys, "admin", Role.ADMIN, None)
    serve.main(["--keys", str(keys), "--db", f"sqlite:///{tmp_path / 'x.db'}"])
    assert seen["access_log"] is False and seen["proxy_headers"] is False


def test_request_log_times_are_marked_as_utc(api, audit):
    api.get("/v1/invariants", headers=auth("auditor"))
    assert audit.requests()[0]["at"].endswith("+00:00")
