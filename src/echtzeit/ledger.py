"""Double-entry ledger with atomic, idempotent transfers.

Rules enforced here:

* A transfer writes exactly two ledger entries (debit and credit) that sum to zero.
* The balance check and both balance updates happen in one database transaction.
  Any failure rolls back everything, so money cannot disappear or appear.
* An idempotency key identifies one logical request. Repeating it with the same
  payload returns the original result. Repeating it with a different payload fails.
* Only completed transfers are stored. A transfer rejected for insufficient funds
  leaves no trace and can be retried with the same key.
"""

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from echtzeit.errors import (
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidRequest,
)
from echtzeit.iso20022.identifiers import bic_is_valid, iban_is_valid
from echtzeit.models import CUSTOMER, SYSTEM, Account, LedgerEntry, Transfer


@dataclass(frozen=True)
class TransferResult:
    transfer_id: str
    idempotency_key: str
    debtor_id: str
    creditor_id: str
    amount_cents: int
    replayed: bool


@dataclass
class InvariantReport:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class AccountInfo:
    id: str
    holder_name: str
    kind: str
    iban: str | None
    bic: str | None
    balance_cents: int


def _request_hash(
    debtor_id: str,
    creditor_id: str,
    amount_cents: int,
    reference: str | None,
    end_to_end_id: str | None,
    uetr: str | None,
) -> str:
    canonical = "|".join(
        [
            debtor_id,
            creditor_id,
            str(amount_cents),
            reference or "",
            end_to_end_id or "",
            uetr or "",
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    def create_account(
        self,
        holder_name: str,
        kind: str = CUSTOMER,
        *,
        iban: str | None = None,
        bic: str | None = None,
    ) -> str:
        if not holder_name or not holder_name.strip():
            raise InvalidRequest("holder_name must not be empty")
        if kind not in (CUSTOMER, SYSTEM):
            raise InvalidRequest(f"unknown account kind: {kind!r}")
        if kind == SYSTEM and (iban is not None or bic is not None):
            # A system account may go negative, so it must never be reachable by a payment message.
            raise InvalidRequest("a system account cannot have an IBAN or BIC")
        if iban is not None and not iban_is_valid(iban):
            raise InvalidRequest("iban is not valid")
        if bic is not None and not bic_is_valid(bic):
            raise InvalidRequest("bic is not valid")
        if (iban is None) != (bic is None):
            raise InvalidRequest("iban and bic must be given together")
        try:
            with self._sessions.begin() as session:
                account = Account(holder_name=holder_name.strip(), kind=kind, iban=iban, bic=bic)
                session.add(account)
                session.flush()
                return account.id
        except IntegrityError as exc:
            raise InvalidRequest("iban is already in use") from exc

    def find_account_by_iban(self, iban: str) -> AccountInfo | None:
        with self._sessions() as session, session.begin():
            account = session.scalar(select(Account).where(Account.iban == iban))
            if account is None:
                return None
            return AccountInfo(
                id=account.id,
                holder_name=account.holder_name,
                kind=account.kind,
                iban=account.iban,
                bic=account.bic,
                balance_cents=account.balance_cents,
            )

    def balance(self, account_id: str) -> int:
        with self._sessions() as session, session.begin():
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFound(account_id)
            return account.balance_cents

    def transfer(
        self,
        *,
        idempotency_key: str,
        debtor_id: str,
        creditor_id: str,
        amount_cents: int,
        reference: str | None = None,
        end_to_end_id: str | None = None,
        uetr: str | None = None,
    ) -> TransferResult:
        self._validate(idempotency_key, debtor_id, creditor_id, amount_cents)
        request_hash = _request_hash(
            debtor_id, creditor_id, amount_cents, reference, end_to_end_id, uetr
        )

        with self._sessions.begin() as session:
            existing = self._find_existing(session, idempotency_key, request_hash)
            if existing is not None:
                return self._result(existing, replayed=True)

            self._require_accounts(session, debtor_id, creditor_id)
            self._move_balances(session, debtor_id, creditor_id, amount_cents)

            transfer = Transfer(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                debtor_id=debtor_id,
                creditor_id=creditor_id,
                amount_cents=amount_cents,
                reference=reference,
                end_to_end_id=end_to_end_id,
                uetr=uetr,
            )
            session.add(transfer)
            session.flush()
            self._write_entries(session, transfer)
            return self._result(transfer, replayed=False)

    def lookup_transfer(
        self,
        *,
        idempotency_key: str,
        debtor_id: str,
        creditor_id: str,
        amount_cents: int,
        reference: str | None = None,
        end_to_end_id: str | None = None,
        uetr: str | None = None,
    ) -> TransferResult | None:
        """Return the stored transfer for this key, or None if it was never completed.

        Raises IdempotencyConflict if the key exists with a different payload. Nothing is
        written. A caller uses this to answer a repeated request before applying any rule
        that depends on the current time, so a late retry of a completed payment never
        reports a failure for money that has moved.
        """
        self._validate(idempotency_key, debtor_id, creditor_id, amount_cents)
        request_hash = _request_hash(
            debtor_id, creditor_id, amount_cents, reference, end_to_end_id, uetr
        )
        with self._sessions() as session, session.begin():
            existing = self._find_existing(session, idempotency_key, request_hash)
            return None if existing is None else self._result(existing, replayed=True)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _find_existing(session: Session, idempotency_key: str, request_hash: str):
        existing = session.scalar(
            select(Transfer).where(Transfer.idempotency_key == idempotency_key)
        )
        if existing is not None and existing.request_hash != request_hash:
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key!r} was used with a different request"
            )
        return existing

    @staticmethod
    def _validate(key: str, debtor_id: str, creditor_id: str, amount_cents: int) -> None:
        if not isinstance(key, str) or not key.strip():
            raise InvalidRequest("idempotency_key must be a non-empty string")
        if len(key) > 128:
            raise InvalidRequest("idempotency_key must be at most 128 characters")
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise InvalidRequest("amount_cents must be an integer")
        if amount_cents <= 0:
            raise InvalidRequest("amount_cents must be greater than zero")
        if debtor_id == creditor_id:
            raise InvalidRequest("debtor and creditor must be different accounts")

    @staticmethod
    def _require_accounts(session: Session, *account_ids: str) -> None:
        found = set(session.scalars(select(Account.id).where(Account.id.in_(account_ids))))
        for account_id in account_ids:
            if account_id not in found:
                raise AccountNotFound(account_id)

    @staticmethod
    def _move_balances(
        session: Session, debtor_id: str, creditor_id: str, amount_cents: int
    ) -> None:
        # Fixed update order (by account id) avoids deadlocks on databases with row locks.
        deltas = sorted(
            [(debtor_id, -amount_cents), (creditor_id, amount_cents)], key=lambda d: d[0]
        )
        for account_id, delta in deltas:
            statement = update(Account).where(Account.id == account_id)
            if delta < 0:
                # The check lives in the UPDATE itself, so it is atomic with the change.
                statement = statement.where(
                    (Account.kind == SYSTEM) | (Account.balance_cents >= -delta)
                )
            result = session.execute(
                statement.values(balance_cents=Account.balance_cents + delta),
                execution_options={"synchronize_session": False},
            )
            if result.rowcount != 1:
                raise InsufficientFunds(f"account {account_id} cannot cover {-delta} cents")

    @staticmethod
    def _write_entries(session: Session, transfer: Transfer) -> None:
        session.add_all(
            [
                LedgerEntry(
                    transfer_id=transfer.id,
                    account_id=transfer.debtor_id,
                    amount_cents=-transfer.amount_cents,
                ),
                LedgerEntry(
                    transfer_id=transfer.id,
                    account_id=transfer.creditor_id,
                    amount_cents=transfer.amount_cents,
                ),
            ]
        )
        session.flush()

    @staticmethod
    def _result(transfer: Transfer, *, replayed: bool) -> TransferResult:
        return TransferResult(
            transfer_id=transfer.id,
            idempotency_key=transfer.idempotency_key,
            debtor_id=transfer.debtor_id,
            creditor_id=transfer.creditor_id,
            amount_cents=transfer.amount_cents,
            replayed=replayed,
        )

    # -- audit -------------------------------------------------------------

    def verify_invariants(self) -> InvariantReport:
        """Recompute everything from the ledger and compare with stored balances."""
        report = InvariantReport()
        with self._sessions() as session, session.begin():
            total = session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)))
            if total != 0:
                report.problems.append(f"ledger entries sum to {total}, expected 0")

            ledger_sums = dict(
                session.execute(
                    select(LedgerEntry.account_id, func.sum(LedgerEntry.amount_cents)).group_by(
                        LedgerEntry.account_id
                    )
                ).all()
            )
            for account in session.scalars(select(Account)):
                expected = ledger_sums.get(account.id, 0)
                if account.balance_cents != expected:
                    report.problems.append(
                        f"account {account.id}: stored balance {account.balance_cents} "
                        f"differs from ledger sum {expected}"
                    )
                if account.kind == CUSTOMER and account.balance_cents < 0:
                    report.problems.append(f"account {account.id}: negative customer balance")

            per_transfer = session.execute(
                select(
                    LedgerEntry.transfer_id,
                    func.count(LedgerEntry.id),
                    func.sum(LedgerEntry.amount_cents),
                ).group_by(LedgerEntry.transfer_id)
            ).all()
            for transfer_id, count, entry_sum in per_transfer:
                if count != 2 or entry_sum != 0:
                    report.problems.append(
                        f"transfer {transfer_id}: {count} entries summing to {entry_sum}"
                    )

            transfer_count = session.scalar(select(func.count(Transfer.id)))
            if transfer_count != len(per_transfer):
                report.problems.append(
                    f"{transfer_count} transfers but {len(per_transfer)} have ledger entries"
                )
        return report
