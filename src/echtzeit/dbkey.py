"""Key files for database encryption at rest.

A key file holds one line: 64 hexadecimal characters, which is a 256-bit raw key for
SQLCipher. Rules:

* The file must not be readable or writable by group or others (mode 600), the same rule as
  for API key files and TLS private keys.
* The key is never part of a database URL, an environment variable value or a log line. Only the
  path to the key file is passed around.
* A new key comes from the operating system's secure random generator (`secrets`).

Create a key:

    python -m echtzeit.dbkey new db.key

Losing the key file means losing the database. Keep a copy somewhere safe and separate from
the database file.
"""

import argparse
import os
import secrets
import stat
from pathlib import Path

KEY_HEX_LENGTH = 64


class DbKeyError(Exception):
    """The key file is missing, too open, or does not hold a valid key."""


def validate_key(key: str) -> str:
    """Return the key in lower case if it is exactly 64 hex characters, else raise."""
    key = key.strip()
    if len(key) != KEY_HEX_LENGTH:
        raise DbKeyError(f"a database key must be {KEY_HEX_LENGTH} hex characters")
    try:
        bytes.fromhex(key)
    except ValueError:
        raise DbKeyError("a database key may contain only hex characters 0-9 and a-f") from None
    return key.lower()


def load_key_file(path: str | Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise DbKeyError(f"database key file {path} does not exist")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DbKeyError(
            f"database key file {path} is accessible by group or others; run: chmod 600 {path}"
        )
    return validate_key(path.read_text(encoding="ascii"))


def write_new_key(path: str | Path) -> None:
    """Create a key file with mode 600. Refuses to overwrite an existing file."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise DbKeyError(f"{path} already exists; refusing to overwrite a key") from None
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(secrets.token_hex(KEY_HEX_LENGTH // 2) + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage database encryption keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a new key file with mode 600")
    new.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        write_new_key(args.path)
    except DbKeyError as exc:
        parser.error(str(exc))
    print(f"wrote a new database key to {args.path}; keep a copy apart from the database")


if __name__ == "__main__":
    main()
