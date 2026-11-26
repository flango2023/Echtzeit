"""Identifier helpers: IBAN, BIC, UETR.

Generated IBANs and BICs have valid formats and check digits but belong to no real bank.
`FICTIONAL_BANK_CODE` and `FICTIONAL_BIC` are placeholders. They are not registered
anywhere, and nobody has checked that no real institution uses them.
"""

import re
import uuid

FICTIONAL_BANK_CODE = "00000000"
FICTIONAL_BIC = "ECHTDEFF"

_IBAN_RE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}")
_BIC_RE = re.compile(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?")
_UETR_RE = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")


def _mod97(text: str) -> int:
    # Letters become two-digit numbers (A=10 ... Z=35).
    return int("".join(str(int(char, 36)) for char in text)) % 97


def iban_is_valid(iban: str) -> bool:
    """True if the IBAN is upper-case, has no spaces and passes the mod-97 check.

    The length per country is checked only for Germany (22 characters), the country of every
    IBAN generated here.
    """
    if not isinstance(iban, str) or not _IBAN_RE.fullmatch(iban):
        return False
    if iban.startswith("DE") and len(iban) != 22:
        return False
    return _mod97(iban[4:] + iban[:4]) == 1


def make_german_iban(account_number: str, bank_code: str = FICTIONAL_BANK_CODE) -> str:
    """Build a German IBAN (DE + 2 check digits + 8-digit bank code + 10-digit account)."""
    if not re.fullmatch(r"[0-9]{8}", bank_code):
        raise ValueError("bank_code must have 8 digits")
    if not re.fullmatch(r"[0-9]{1,10}", account_number):
        raise ValueError("account_number must have 1 to 10 digits")
    bban = bank_code + account_number.zfill(10)
    check = 98 - _mod97(bban + "DE00")
    return f"DE{check:02d}{bban}"


def bic_is_valid(bic: str) -> bool:
    return isinstance(bic, str) and bool(_BIC_RE.fullmatch(bic))


def new_uetr() -> str:
    """A random version 4 UUID in lower case, as ISO 20022 UUIDv4Identifier requires."""
    return str(uuid.uuid4())


def uetr_is_valid(uetr: str) -> bool:
    return isinstance(uetr, str) and bool(_UETR_RE.fullmatch(uetr))
