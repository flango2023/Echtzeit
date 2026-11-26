"""Database encryption at rest (SQLCipher) and its key files."""

import importlib.util
import os
import stat

import pytest
from helpers import BANK_A, BANK_B
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from echtzeit.db import EncryptionKeyError, make_engine, make_session_factory
from echtzeit.dbkey import DbKeyError, load_key_file, main, validate_key, write_new_key
from echtzeit.errors import IdempotencyConflict
from echtzeit.iso20022.identifiers import make_german_iban
from echtzeit.ledger import Ledger
from echtzeit.models import SYSTEM, init_db

needs_sqlcipher = pytest.mark.skipif(
    importlib.util.find_spec("sqlcipher3") is None, reason="sqlcipher3 is not installed"
)

KEY = "0f" * 32
OTHER_KEY = "a1" * 32
IBAN = make_german_iban("4242")


# -- key files -------------------------------------------------------------------------------


def test_a_new_key_file_is_private_random_and_never_overwritten(tmp_path):
    path = tmp_path / "db.key"
    write_new_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    key = load_key_file(path)
    assert len(key) == 64 and bytes.fromhex(key)
    with pytest.raises(DbKeyError, match="refusing to overwrite"):
        write_new_key(path)
    assert load_key_file(path) == key
    second = tmp_path / "second.key"
    write_new_key(second)
    assert load_key_file(second) != key


def test_the_key_file_cli_creates_a_key(tmp_path, capsys):
    path = tmp_path / "cli.key"
    main(["new", str(path)])
    assert load_key_file(path)
    assert "wrote a new database key" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["new", str(path)])


def test_key_files_that_are_open_missing_or_malformed_are_refused(tmp_path):
    path = tmp_path / "db.key"
    with pytest.raises(DbKeyError, match="does not exist"):
        load_key_file(path)
    path.write_text(KEY + "\n")
    for mode in (0o640, 0o604, 0o660):
        os.chmod(path, mode)
        with pytest.raises(DbKeyError, match="chmod 600"):
            load_key_file(path)
    os.chmod(path, 0o600)
    assert load_key_file(path) == KEY
    for bad in ("", "ab" * 31, "ab" * 33, "zz" * 32, "0f" * 31 + '"x'):
        path.write_text(bad)
        with pytest.raises(DbKeyError):
            load_key_file(path)


def test_only_hex_keys_reach_the_pragma():
    assert validate_key(KEY.upper()) == KEY
    # 64 characters each, so only the character check can refuse them.
    for bad in ("'\"; --" + "0" * 58, "ab" * 30 + "  " + "cd", "0x" + "ab" * 31):
        assert len(bad) == 64
        with pytest.raises(DbKeyError):
            validate_key(bad)
        with pytest.raises(DbKeyError):
            make_engine("sqlite:///unused.db", key=bad)


def test_a_key_file_that_is_not_ascii_is_refused(tmp_path):
    path = tmp_path / "db.key"
    path.write_bytes(b"\xff" * 64)
    os.chmod(path, 0o600)
    with pytest.raises(DbKeyError, match="ASCII"):
        load_key_file(path)


# -- encrypted database ----------------------------------------------------------------------


def _fill(engine) -> tuple[Ledger, str, str]:
    init_db(engine)
    ledger = Ledger(make_session_factory(engine))
    bank = ledger.create_account("Echtzeit Funding", kind=SYSTEM)
    alice = ledger.create_account("Alice Example", iban=IBAN, bic=BANK_A)
    bob = ledger.create_account("Bob Example", iban=make_german_iban("4343"), bic=BANK_B)
    ledger.transfer(idempotency_key="fund", debtor_id=bank, creditor_id=alice, amount_cents=5000)
    ledger.transfer(idempotency_key="pay", debtor_id=alice, creditor_id=bob, amount_cents=1200)
    return ledger, alice, bob


def _raw(path) -> bytes:
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


@needs_sqlcipher
def test_the_ledger_works_on_an_encrypted_file_and_nothing_is_readable_on_disk(tmp_path):
    path = tmp_path / "enc.db"
    engine = make_engine(f"sqlite:///{path}", key=KEY)
    ledger, alice, _ = _fill(engine)
    assert ledger.verify_invariants().ok
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA temp_store").scalar() == 2  # memory
    raw = _raw(path)  # read while the write-ahead log still holds pages
    engine.dispose()
    raw += path.read_bytes()
    for secret in (b"SQLite format 3", IBAN.encode(), b"Alice Example", b"ledger_entries"):
        assert secret not in raw

    # Control: the same data without a key is readable on disk, so the check above can fail.
    plain_path = tmp_path / "plain.db"
    plain = make_engine(f"sqlite:///{plain_path}")
    _fill(plain)
    plain.dispose()
    plain_raw = plain_path.read_bytes()
    assert b"SQLite format 3" in plain_raw and IBAN.encode() in plain_raw

    reopened = make_engine(f"sqlite:///{path}", key=KEY)
    again = Ledger(make_session_factory(reopened))
    assert again.verify_invariants().ok
    info = again.account_info(alice)
    assert (info.iban, info.balance_cents) == (IBAN, 3800)
    reopened.dispose()


@needs_sqlcipher
def test_a_wrong_key_or_no_key_cannot_open_an_encrypted_file(tmp_path):
    path = tmp_path / "enc.db"
    engine = make_engine(f"sqlite:///{path}", key=KEY)
    _fill(engine)
    engine.dispose()
    with pytest.raises(EncryptionKeyError, match="wrong key"):
        make_engine(f"sqlite:///{path}", key=OTHER_KEY).connect()
    with pytest.raises(DatabaseError), make_engine(f"sqlite:///{path}").connect() as connection:
        connection.execute(text("SELECT count(*) FROM sqlite_master")).scalar()


@needs_sqlcipher
def test_a_key_cannot_open_an_unencrypted_file(tmp_path):
    path = tmp_path / "plain.db"
    plain = make_engine(f"sqlite:///{path}")
    _fill(plain)
    plain.dispose()
    with pytest.raises(EncryptionKeyError):
        make_engine(f"sqlite:///{path}", key=KEY).connect()


@needs_sqlcipher
def test_database_rules_still_hold_under_sqlcipher(tmp_path):
    """Errors from sqlcipher3 must map to SQLAlchemy errors, or idempotency and triggers break."""
    engine = make_engine(f"sqlite:///{tmp_path / 'enc.db'}", key=KEY)
    ledger, alice, bob = _fill(engine)
    with pytest.raises(IdempotencyConflict):
        ledger.transfer(idempotency_key="pay", debtor_id=alice, creditor_id=bob, amount_cents=1)
    with pytest.raises(DatabaseError, match="append-only"), engine.begin() as connection:
        connection.execute(text("UPDATE ledger_entries SET amount_cents = 0"))
    assert ledger.verify_invariants().ok
    engine.dispose()


def test_encryption_needs_a_file_database():
    for url in ("sqlite://", "sqlite:///:memory:", "postgresql://localhost/echtzeit"):
        with pytest.raises(ValueError, match="file-based"):
            make_engine(url, key=KEY)


@needs_sqlcipher
def test_the_api_opens_an_encrypted_database_from_the_environment(monkeypatch, tmp_path):
    from echtzeit.api.app import create_app_from_env
    from echtzeit.api.auth import Role, add_key

    keys = tmp_path / "keys.json"
    add_key(keys, "bank-a", Role.PSP, BANK_A)
    db_key = tmp_path / "db.key"
    write_new_key(db_key)
    path = tmp_path / "api.db"
    monkeypatch.setenv("ECHTZEIT_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("ECHTZEIT_DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("ECHTZEIT_DB_KEY_FILE", str(db_key))
    create_app_from_env()
    assert path.exists() and b"SQLite format 3" not in path.read_bytes()
    with pytest.raises(EncryptionKeyError):
        make_engine(f"sqlite:///{path}", key=OTHER_KEY).connect()
    wrong = tmp_path / "wrong.key"
    write_new_key(wrong)
    monkeypatch.setenv("ECHTZEIT_DB_KEY_FILE", str(wrong))
    with pytest.raises(EncryptionKeyError):
        create_app_from_env()  # a wrong key stops the server at start
    monkeypatch.setenv("ECHTZEIT_DB_KEY_FILE", str(db_key))
    os.chmod(db_key, 0o644)
    with pytest.raises(DbKeyError):
        create_app_from_env()
