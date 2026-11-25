"""Engine and session setup.

SQLite is used for version 1. Two settings make it behave predictably for a ledger:

* Every transaction starts with BEGIN IMMEDIATE, so writers are serialized and a
  balance check and its update cannot interleave with another writer.
* Foreign keys are switched on, because SQLite ignores them by default.

Limit: SQLite allows one writer at a time. Tests here prove the ledger rules hold
under concurrent threads, but they say nothing about PostgreSQL row-lock behaviour.
"""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str = "sqlite:///echtzeit.db") -> Engine:
    is_sqlite = url.startswith("sqlite")
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
