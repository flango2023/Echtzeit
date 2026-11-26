"""Engine and session setup.

SQLite is used for version 1. Two settings make it behave predictably for a ledger:

* Every transaction starts with BEGIN IMMEDIATE, so writers are serialized and a
  balance check and its update cannot interleave with another writer.
* Foreign keys are switched on, because SQLite ignores them by default.

Limit: SQLite allows one writer at a time. Tests here prove the ledger rules hold
under concurrent threads, but they say nothing about PostgreSQL row-lock behaviour.

Encryption at rest is optional. With a key, the engine opens the file through SQLCipher
(the `sqlcipher3` package, installed with the `encryption` extra), which encrypts every page of
the database and of its write-ahead log. The key is set inside the connection factory, before
SQLAlchemy runs any query of its own, and a wrong key is detected at once. What SQLCipher does
not cover: data in memory while the process runs, copies an operator exports, and the key file
itself, which must be protected by file permissions (see `echtzeit.dbkey`).
"""

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class EncryptionKeyError(Exception):
    """The database could not be opened with the given key: wrong key, or not encrypted."""


def _encrypted_engine(url: str, key: str) -> Engine:
    from .dbkey import validate_key

    key = validate_key(key)  # only hex digits, so the PRAGMA below cannot be injected
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        raise ValueError("encryption needs a file-based sqlite URL, e.g. sqlite:///echtzeit.db")
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError:
        raise RuntimeError(
            "database encryption needs the sqlcipher3 package: pip install 'echtzeit[encryption]'"
        ) from None
    path = parsed.database

    def connect():
        connection = sqlcipher.connect(path, timeout=30, check_same_thread=False)
        try:
            connection.execute(f"PRAGMA key = \"x'{key}'\"")
            # SQLCipher accepts any key at PRAGMA time; the first read reveals a wrong one.
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            # SQLCipher's design notes count on temporary data staying in memory, not in files.
            connection.execute("PRAGMA temp_store = MEMORY")
        except sqlcipher.DatabaseError:
            connection.close()
            raise EncryptionKeyError(
                f"cannot open {path}: wrong key, or the file is not an encrypted database"
            ) from None
        return connection

    # module= makes SQLAlchemy map sqlcipher3 errors (IntegrityError and others) correctly.
    return create_engine(url, module=sqlcipher, creator=connect)


def make_engine(url: str = "sqlite:///echtzeit.db", *, key: str | None = None) -> Engine:
    """Create an engine. `key` (64 hex characters) turns on SQLCipher encryption at rest."""
    is_sqlite = url.startswith("sqlite")
    if key is not None:
        engine = _encrypted_engine(url, key)
    else:
        connect_args = {"timeout": 30} if is_sqlite else {}
        engine = create_engine(url, connect_args=connect_args)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection, _record):
            # Take transaction control away from the sqlite3 module so that
            # the "begin" event below decides when and how a transaction starts.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _on_begin(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
