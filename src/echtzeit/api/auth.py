"""API keys, roles, expiry and rotation.

Keys are random (32 bytes from `secrets`), shown once when created and stored only as SHA-256
hashes. A plain hash is enough because the keys are high-entropy random values, not passwords
people choose. Comparison uses `hmac.compare_digest`.

Every key has an id and an expiry time. A client may hold several keys at once, which is how
rotation works: create a new key, switch the client over, revoke the old key. The server
re-reads the key file when it changes, so a revocation takes effect without a restart. If the
changed file is invalid, every key is refused until it is fixed (fail closed).

Roles:

* psp: a bank. Bound to one BIC. Acts only for its own customers' accounts.
* auditor: read-only access to everything, including the audit logs and invariant checks.
* admin: creates accounts and funds them with fictional money. Cannot send payments or
  place holds.

Commands (the key file is created with mode 600 and must stay out of version control):

    python -m echtzeit.api.auth new --client bank-a --role psp --bic ECHTDEFF --keys keys.json
    python -m echtzeit.api.auth list --keys keys.json
    python -m echtzeit.api.auth revoke --key-id <id> --keys keys.json
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from echtzeit.iso20022.identifiers import bic_is_valid

KEY_PREFIX = "ez_"
DEFAULT_VALID_DAYS = 90
MAX_VALID_DAYS = 365
RELOAD_CHECK_SECONDS = 2.0


class Role(Enum):
    PSP = "psp"
    AUDITOR = "auditor"
    ADMIN = "admin"


@dataclass(frozen=True)
class Client:
    client_id: str
    role: Role
    bic: str | None = None  # set for role psp only
    key_id: str | None = None  # the key used for this request

    def acts_for(self, bic: str | None) -> bool:
        """True if this client is the bank with this BIC."""
        return self.role is Role.PSP and bic is not None and self.bic == bic


@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    digest: str
    client: Client
    expires_at: datetime


logger = logging.getLogger("echtzeit.api")


class KeyFileError(Exception):
    """The key file is missing, unsafe or malformed."""


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _parse_time(value: object, client_id: str) -> datetime:
    if not isinstance(value, str):
        raise KeyFileError(f"key of {client_id!r} needs expires_at as an ISO 8601 time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KeyFileError(f"expires_at of {client_id!r} is not an ISO 8601 time") from exc
    if moment.tzinfo is None:
        raise KeyFileError(f"expires_at of {client_id!r} needs a time zone")
    try:
        return moment.astimezone(UTC)
    except OverflowError as exc:
        raise KeyFileError(f"expires_at of {client_id!r} is out of range") from exc


def _check_entry(entry: object) -> KeyEntry:
    if not isinstance(entry, dict):
        raise KeyFileError("each key entry must be a JSON object")
    try:
        client_id = entry["client_id"]
        role = Role(entry["role"])
        digest = entry["key_sha256"]
        key_id = entry["key_id"]
    except (KeyError, ValueError, TypeError) as exc:
        raise KeyFileError(f"malformed key entry: {exc}") from exc
    if not isinstance(client_id, str) or not client_id or len(client_id) > 100:
        raise KeyFileError("client_id must be a non-empty string of at most 100 characters")
    if not isinstance(key_id, str) or not 8 <= len(key_id) <= 32 or not key_id.isalnum():
        raise KeyFileError(f"key_id of {client_id!r} must be 8 to 32 letters or digits")
    if not isinstance(digest, str) or len(digest) != 64:
        raise KeyFileError(f"key_sha256 of {client_id!r} must be a SHA-256 hex digest")
    bic = entry.get("bic")
    if role is Role.PSP:
        if not bic_is_valid(bic):
            raise KeyFileError(f"client {client_id!r} has role psp and needs a valid bic")
    elif bic is not None:
        raise KeyFileError(f"client {client_id!r} has role {role.value} and must not have a bic")
    expires_at = _parse_time(entry.get("expires_at"), client_id)
    return KeyEntry(key_id, digest, Client(client_id, role, bic), expires_at)


def _check_entries(raw: object) -> list[KeyEntry]:
    if not isinstance(raw, list):
        raise KeyFileError("key file must contain a JSON list")
    entries = [_check_entry(item) for item in raw]
    ids: set[str] = set()
    clients: dict[str, Client] = {}
    for entry in entries:
        if entry.key_id in ids:
            raise KeyFileError(f"duplicate key_id {entry.key_id!r}")
        ids.add(entry.key_id)
        known = clients.setdefault(entry.client.client_id, entry.client)
        if (known.role, known.bic) != (entry.client.role, entry.client.bic):
            raise KeyFileError(
                f"all keys of client {entry.client.client_id!r} must have the same role and bic"
            )
    return entries


def _load_file(path: Path) -> object:
    """The parsed JSON of a key file, after the existence and mode checks."""
    if not path.is_file():
        raise KeyFileError(f"key file {path} does not exist")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(
            f"key file {path} is accessible by group or others; run: chmod 600 {path}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyFileError(f"key file {path} cannot be read as UTF-8 text") from exc
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise KeyFileError(f"key file {path} is not valid JSON") from exc


def _read_file(path: Path) -> list[KeyEntry]:
    return _check_entries(_load_file(path))


class KeyStore:
    def __init__(
        self,
        entries: list[dict],
        *,
        path: Path | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self._entries = _check_entries(entries)
        self._path = path
        self._signature = self._file_signature() if path else None
        self._monotonic = monotonic or time.monotonic
        self._next_check = 0.0
        self._broken = False
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "KeyStore":
        path = Path(path)
        return cls(_load_file(path), path=path, **kwargs)  # checks the file mode too

    def _file_signature(self) -> tuple[int, int, int] | None:
        try:
            info = self._path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def _refresh(self) -> None:
        """Re-read the key file if it changed. An invalid file refuses every key."""
        if self._path is None:
            return
        with self._lock:
            now = self._monotonic()
            if now < self._next_check:
                return
            self._next_check = now + RELOAD_CHECK_SECONDS
            signature = self._file_signature()
            if signature == self._signature:
                return
            self._signature = signature
            try:
                self._entries = _read_file(self._path)
                self._broken = False
            except Exception:  # any failure fails closed, not only a KeyFileError
                # The file holds only hashes, so the error text reveals no key.
                logger.exception("key file %s could not be loaded; refusing every key", self._path)
                self._entries = []
                self._broken = True

    @property
    def broken(self) -> bool:
        """True after the key file changed into something invalid."""
        return self._broken

    def authenticate(self, raw_key: str | None, now: datetime | None = None) -> Client | None:
        self._refresh()
        if not raw_key or not raw_key.startswith(KEY_PREFIX) or len(raw_key) > 200:
            return None
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        digest = hash_key(raw_key)
        found = None
        for entry in self._entries:  # check every entry, no early exit
            if hmac.compare_digest(entry.digest, digest) and moment < entry.expires_at:
                found = entry
        if found is None:
            return None
        client = found.client
        return Client(client.client_id, client.role, client.bic, found.key_id)


def _load_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    _read_file(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_raw(path: Path, entries: list[dict]) -> None:
    _check_entries(entries)  # validate before writing
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)  # atomic, so the server never reads half a file


def add_key(
    path: Path,
    client_id: str,
    role: Role,
    bic: str | None,
    *,
    valid_days: int = DEFAULT_VALID_DAYS,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Create a key and add its hash to the file. Returns (key, key_id). The key is shown once."""
    if not 1 <= valid_days <= MAX_VALID_DAYS:
        raise KeyFileError(f"valid_days must be between 1 and {MAX_VALID_DAYS}")
    entries = _load_raw(path)
    key = new_key()
    key_id = secrets.token_hex(8)
    expires = (now or datetime.now(UTC)) + timedelta(days=valid_days)
    entry = {
        "client_id": client_id,
        "role": role.value,
        "key_id": key_id,
        "key_sha256": hash_key(key),
        "expires_at": expires.astimezone(UTC).isoformat(timespec="seconds"),
    }
    if bic is not None:
        entry["bic"] = bic
    _write_raw(path, [*entries, entry])
    return key, key_id


def revoke_key(path: Path, key_id: str) -> None:
    entries = _load_raw(path)
    remaining = [entry for entry in entries if entry.get("key_id") != key_id]
    if len(remaining) == len(entries):
        raise KeyFileError(f"no key with key_id {key_id!r}")
    _write_raw(path, remaining)


def list_keys(path: Path) -> list[dict]:
    """Key metadata without hashes."""
    return [
        {k: entry.get(k) for k in ("client_id", "role", "bic", "key_id", "expires_at")}
        for entry in _load_raw(path)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Echtzeit API keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a key and print it once")
    new.add_argument("--client", required=True)
    new.add_argument("--role", required=True, choices=[role.value for role in Role])
    new.add_argument("--bic")
    new.add_argument("--valid-days", type=int, default=DEFAULT_VALID_DAYS)
    new.add_argument("--keys", required=True, type=Path)
    revoke = sub.add_parser("revoke", help="remove a key")
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--keys", required=True, type=Path)
    show = sub.add_parser("list", help="show key metadata, never the keys")
    show.add_argument("--keys", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "new":
        key, key_id = add_key(
            args.keys, args.client, Role(args.role), args.bic, valid_days=args.valid_days
        )
        print(key)  # stdout: only the key, so `> file` captures exactly the key
        print(f"key_id: {key_id}", file=sys.stderr)
    elif args.command == "revoke":
        revoke_key(args.keys, args.key_id)
        print(f"revoked {args.key_id}")
    else:
        print(json.dumps(list_keys(args.keys), indent=2))


if __name__ == "__main__":
    main()
