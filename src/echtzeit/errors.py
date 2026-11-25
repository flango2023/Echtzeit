"""Domain errors raised by the ledger."""


class EchtzeitError(Exception):
    """Base class for all Echtzeit errors."""


class InvalidRequest(EchtzeitError):
    """The request is malformed (bad amount, same debtor and creditor, empty key)."""


class AccountNotFound(EchtzeitError):
    """A referenced account does not exist."""


class InsufficientFunds(EchtzeitError):
    """The debtor balance is lower than the transfer amount."""


class IdempotencyConflict(EchtzeitError):
    """An idempotency key was reused with a different request payload."""
