"""Verification of Payee (VoP): does the name the payer typed belong to the IBAN?

The four outcomes are the ones the EPC VoP scheme defines (EPC FAQ "Clarifications about the
provision of VOP-services for bulk files"): match, close match, no match, verification not
possible. The payer decides whether to go ahead after any of them.

How names are compared is this project's choice. The EPC scheme leaves the matching method
to the responding PSP. Here:

1. Both names are normalised: Unicode compatibility form, accents removed, case folded
   (so "ß" becomes "ss"), punctuation turned into spaces, words sorted.
2. Equal after normalisation: match.
3. Similarity of at least CLOSE_MATCH_THRESHOLD (difflib ratio): close match. The simulator
   then returns the account holder's name, so the payer can see it.
4. Otherwise: no match.
5. IBAN not held by a known account: verification not possible.

The threshold is not calibrated on real names. It only orders the simulated cases.
"""

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from echtzeit.ledger import Ledger

CLOSE_MATCH_THRESHOLD = 0.85


class VopOutcome(Enum):
    MATCH = "MATCH"
    CLOSE_MATCH = "CLOSE_MATCH"
    NO_MATCH = "NO_MATCH"
    NOT_POSSIBLE = "NOT_POSSIBLE"


@dataclass(frozen=True)
class VopResult:
    outcome: VopOutcome
    similarity: float
    suggested_name: str | None = None  # the holder's name, only for a close match


# Latin letters that Unicode does not decompose into a base letter and an accent.
_LATIN_FOLDS = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "æ": "ae", "œ": "oe", "þ": "th"})


def normalise(name: str) -> str:
    """Case-fold, drop accents on Latin letters, keep the marks other scripts need."""
    text = unicodedata.normalize("NFKD", name.casefold()).translate(_LATIN_FOLDS)
    kept: list[str] = []
    for char in text:
        if unicodedata.combining(char) and kept and kept[-1].isascii():
            continue  # an accent on a Latin letter
        is_mark = unicodedata.category(char).startswith("M")  # e.g. Devanagari vowel signs
        kept.append(char if char.isalnum() or is_mark else " ")
    return " ".join(sorted("".join(kept).split()))


def compare_names(typed: str, holder: str) -> tuple[VopOutcome, float]:
    a, b = normalise(typed), normalise(holder)
    if not a or not b:
        return VopOutcome.NO_MATCH, 0.0
    if a == b:
        return VopOutcome.MATCH, 1.0
    similarity = SequenceMatcher(None, a, b).ratio()
    if similarity >= CLOSE_MATCH_THRESHOLD:
        return VopOutcome.CLOSE_MATCH, similarity
    return VopOutcome.NO_MATCH, similarity


def verify_payee(ledger: Ledger, iban: str, typed_name: str) -> VopResult:
    account = ledger.find_account_by_iban(iban)
    if account is None:
        return VopResult(VopOutcome.NOT_POSSIBLE, 0.0)
    outcome, similarity = compare_names(typed_name, account.holder_name)
    suggestion = account.holder_name if outcome is VopOutcome.CLOSE_MATCH else None
    return VopResult(outcome, similarity, suggestion)
