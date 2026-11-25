"""API keys, roles and what each role may do.

Keys are random (32 bytes from `secrets`), shown once when created and stored only as SHA-256
hashes. A plain hash is enough here because the keys are high-entropy random values, not
passwords people choose. Comparison uses `hmac.compare_digest`.

Roles:

* psp: a bank. Bound to one BIC. Acts only for its own customers' accounts.
* auditor: read-only access to everything, including the audit log and invariant checks.
* admin: creates accounts and funds them with fictional money. Cannot send payments or
  place holds.

Create a key:

    python -m echtzeit.api.auth new --client bank-a --role psp --bic ECHTDEFF --keys keys.json

The command prints the key once and adds the hash to the key file. Keep the key file out of
version control and readable only by its owner; the server refuses to start otherwise.
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from echtzeit.iso20022.identifiers import bic_is_valid

KEY_PREFIX = "ez_"


class Role(Enum):
    PSP = "psp"
    AUDITOR = "auditor"
    ADMIN = "admin"


@dataclass(frozen=True)
class Client:
    client_id: str
    role: Role
    bic: str | None = None  # set for role psp only

    def acts_for(self, bic: str | None) -> bool:
        """True if this client is the bank with this BIC."""
        return self.role is Role.PSP and bic is not None and self.bic == bic


class KeyFileError(Exception):
    """The key file is missing, unsafe or malformed."""


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _check_entry(entry: dict) -> tuple[str, Client]:
    try:
        client_id = entry["client_id"]
        role = Role(entry["role"])
        digest = entry["key_sha256"]
    except (KeyError, ValueError, TypeError) as exc:
        raise KeyFileError(f"malformed key entry: {exc}") from exc
    if not isinstance(client_id, str) or not client_id or len(client_id) > 100:
        raise KeyFileError("client_id must be a non-empty string of at most 100 characters")
    if not isinstance(digest, str) or len(digest) != 64:
        raise KeyFileError(f"key_sha256 of {client_id!r} must be a SHA-256 hex digest")
    bic = entry.get("bic")
    if role is Role.PSP:
        if not bic_is_valid(bic):
            raise KeyFileError(f"client {client_id!r} has role psp and needs a valid bic")
    elif bic is not None:
        raise KeyFileError(f"client {client_id!r} has role {role.value} and must not have a bic")
    return digest, Client(client_id, role, bic)


class KeyStore:
    def __init__(self, entries: list[dict]):
        self._entries: list[tuple[str, Client]] = []
        seen: set[str] = set()
        for entry in entries:
            digest, client = _check_entry(entry)
            if client.client_id in seen:
                raise KeyFileError(f"duplicate client_id {client.client_id!r}")
            seen.add(client.client_id)
            self._entries.append((digest, client))

    @classmethod
    def from_file(cls, path: str | Path) -> "KeyStore":
        path = Path(path)
        if not path.is_file():
            raise KeyFileError(f"key file {path} does not exist")
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeyFileError(
                f"key file {path} is accessible by group or others; run: chmod 600 {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KeyFileError(f"key file {path} is not valid JSON") from exc
        if not isinstance(data, list):
            raise KeyFileError("key file must contain a JSON list")
        return cls(data)

    def authenticate(self, raw_key: str | None) -> Client | None:
        if not raw_key or not raw_key.startswith(KEY_PREFIX) or len(raw_key) > 200:
            return None
        digest = hash_key(raw_key)
        found = None
        for stored, client in self._entries:  # check every entry, no early exit
            if hmac.compare_digest(stored, digest):
                found = client
        return found


def add_key(path: Path, client_id: str, role: Role, bic: str | None) -> str:
    """Create a key, add its hash to the file (created with mode 600) and return the key."""
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    key = new_key()
    entry = {"client_id": client_id, "role": role.value, "key_sha256": hash_key(key)}
    if bic is not None:
        entry["bic"] = bic
    KeyStore([*entries, entry])  # validates the result before writing
    entries.append(entry)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)
    os.chmod(path, 0o600)
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Echtzeit API keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a key and print it once")
    new.add_argument("--client", required=True)
    new.add_argument("--role", required=True, choices=[role.value for role in Role])
    new.add_argument("--bic")
    new.add_argument("--keys", required=True, type=Path)
    args = parser.parse_args()
    key = add_key(args.keys, args.client, Role(args.role), args.bic)
    print(key)


if __name__ == "__main__":
    main()
