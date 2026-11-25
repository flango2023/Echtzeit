"""HTTP API for the simulator. Every request needs an API key, except the health check.

Run locally only (see the README):

    ECHTZEIT_DATABASE_URL=sqlite:///echtzeit.db ECHTZEIT_API_KEYS_FILE=keys.json \\
        uvicorn --factory echtzeit.api.app:create_app_from_env --host 127.0.0.1

Authorization rules, all enforced here:

* A psp client acts only for its own BIC. It sends payments only as the debtor agent, reads
  and holds only its own customers' accounts, and freezes traced money only on its own
  accounts. Another bank's account is answered as "not found", so a bank cannot learn which
  IBANs other banks hold (Verification of Payee is the exception, by design of the scheme).
* An auditor reads everything and changes nothing.
* An admin creates and funds accounts and does nothing else.

Who placed or released a hold is the authenticated client id, never a value from the request.
"""

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from echtzeit.api.auth import Client, KeyStore, Role
from echtzeit.api.ratelimit import RateLimiter
from echtzeit.audit import AuditLog
from echtzeit.db import make_engine, make_session_factory
from echtzeit.errors import (
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidRequest,
    MessageError,
    TransferNotFound,
)
from echtzeit.iso20022 import pacs008
from echtzeit.ledger import AccountInfo, Ledger
from echtzeit.models import init_db
from echtzeit.payments import InstantPaymentProcessor
from echtzeit.risk import RiskScorer
from echtzeit.tracing import DEFAULT_MAX_DEPTH, Attribution, freeze_traced_funds, trace_payment
from echtzeit.vop import verify_payee

FUNDING_ACCOUNT = "Echtzeit Funding"
MAX_BODY_BYTES = 64 * 1024
MAX_TRACE_DEPTH = 10


# -- request bodies ----------------------------------------------------------------------------


class _Body(BaseModel):
    # Unknown fields are refused, so a client cannot try to set placed_by or similar.
    model_config = ConfigDict(extra="forbid")


class AccountIn(_Body):
    holder_name: str = Field(min_length=1, max_length=70)
    iban: str = Field(min_length=15, max_length=34)
    bic: str = Field(min_length=8, max_length=11)


class DepositIn(_Body):
    amount_cents: int = Field(gt=0, le=99_999_999_999, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=100)


class VopIn(_Body):
    iban: str = Field(min_length=15, max_length=34)
    name: str = Field(min_length=1, max_length=70)


class AssessmentIn(_Body):
    debtor_iban: str = Field(min_length=15, max_length=34)
    creditor_iban: str = Field(min_length=15, max_length=34)
    name: str = Field(min_length=1, max_length=70)
    amount_cents: int = Field(gt=0, le=99_999_999_999, strict=True)


class HoldIn(_Body):
    amount_cents: int = Field(gt=0, le=99_999_999_999, strict=True)
    reason: str = Field(min_length=1, max_length=200)
    uetr: str | None = Field(default=None, max_length=36)
    mode: str = Field(default="exact", pattern="^(exact|available)$")


class ReleaseIn(_Body):
    reason: str = Field(min_length=1, max_length=200)


class FreezeIn(_Body):
    attribution: Attribution
    reason: str = Field(min_length=1, max_length=200)
    max_depth: int = Field(default=DEFAULT_MAX_DEPTH, ge=0, le=MAX_TRACE_DEPTH, strict=True)


# -- helpers -----------------------------------------------------------------------------------


def problem(status: int, title: str, detail: str | None = None, **extra) -> JSONResponse:
    """RFC 9457 problem details. Never includes internals."""
    body = {"type": "about:blank", "title": title, "status": status, **extra}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class BodyLimit:
    """Refuse request bodies above a size, also when no Content-Length header is sent."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await problem(413, "Request body too large")(scope, receive, send)
            return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await problem(413, "Request body too large")(scope, receive, send)


class _TooLarge(Exception):
    pass


class SecurityHeaders:
    """Add headers to every response, including error responses from outer middleware."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4()).encode()

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", request_id),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        await self.app(scope, receive, with_headers)


def _account_view(account: AccountInfo) -> dict:
    return {
        "iban": account.iban,
        "bic": account.bic,
        "holder_name": account.holder_name,
        "balance_cents": account.balance_cents,
        "held_cents": account.held_cents,
        "available_cents": account.available_cents,
    }


def _hold_view(hold) -> dict:
    return {
        "id": hold.id,
        "requested_cents": hold.requested_cents,
        "amount_cents": hold.amount_cents,
        "reason": hold.reason,
        "placed_by": hold.placed_by,
        "uetr": hold.uetr,
    }


# -- application -------------------------------------------------------------------------------


def create_app(
    ledger: Ledger,
    audit: AuditLog,
    keys: KeyStore,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    limiter: RateLimiter | None = None,
    failed_auth_limiter: RateLimiter | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> FastAPI:
    app = FastAPI(
        title="Echtzeit simulator API",
        version="0.1.0",
        description="Fictional money only. Not for use with real payments or personal data.",
        docs_url=None,  # the interactive pages load scripts from a CDN
        redoc_url=None,
    )
    processor = InstantPaymentProcessor(ledger, audit=audit, clock=clock)
    scorer = RiskScorer(ledger)
    funding = ledger.ensure_system_account(FUNDING_ACCOUNT)
    limiter = limiter or RateLimiter(capacity=120, refill_per_second=20)
    failed_auth_limiter = failed_auth_limiter or RateLimiter(capacity=10, refill_per_second=0.2)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        response = problem(exc.status_code, str(exc.detail))
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Report where the problem is and what is wrong, without echoing the submitted values.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return problem(422, "Invalid request", errors=errors)

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, _exc: Exception):
        return problem(500, "Internal error")

    def client(request: Request) -> Client:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        found = keys.authenticate(token.strip()) if scheme.lower() == "bearer" else None
        if found is None:
            source = request.client.host if request.client else "unknown"
            wait = failed_auth_limiter.acquire(f"failed:{source}")
            if wait:
                raise HTTPException(429, "Too many failed attempts", {"Retry-After": f"{wait:.0f}"})
            raise HTTPException(401, "Missing or invalid API key", {"WWW-Authenticate": "Bearer"})
        wait = limiter.acquire(found.client_id)
        if wait:
            raise HTTPException(
                429, "Rate limit exceeded", {"Retry-After": f"{max(1, round(wait))}"}
            )
        return found

    Caller = Annotated[Client, Depends(client)]

    def require(caller: Client, *roles: Role) -> None:
        if caller.role not in roles:
            raise HTTPException(403, "Not allowed for this role")

    def visible_account(caller: Client, iban: str) -> AccountInfo:
        account = ledger.find_account_by_iban(iban)
        if account is None:
            raise HTTPException(404, "Account not found")
        if caller.role is Role.PSP and not caller.acts_for(account.bic):
            raise HTTPException(404, "Account not found")  # do not reveal other banks' accounts
        return account

    def own_account(caller: Client, iban: str) -> AccountInfo:
        require(caller, Role.PSP)
        return visible_account(caller, iban)

    # -- domain errors -------------------------------------------------------------------------

    @app.exception_handler(MessageError)
    async def message_error(_request: Request, exc: MessageError):
        return problem(400, "Invalid payment message", problems=exc.problems)

    @app.exception_handler(InsufficientFunds)
    async def insufficient(_request: Request, _exc: InsufficientFunds):
        return problem(409, "Not enough spendable money")

    @app.exception_handler(IdempotencyConflict)
    async def conflict(_request: Request, _exc: IdempotencyConflict):
        return problem(409, "Idempotency key or UETR already used with different content")

    @app.exception_handler(InvalidRequest)
    async def invalid(_request: Request, exc: InvalidRequest):
        return problem(422, "Invalid request", str(exc))

    @app.exception_handler(AccountNotFound)
    async def account_missing(_request: Request, _exc: AccountNotFound):
        return problem(404, "Account not found")

    @app.exception_handler(TransferNotFound)
    async def payment_missing(_request: Request, _exc: TransferNotFound):
        return problem(404, "Payment not found")

    # -- endpoints -----------------------------------------------------------------------------

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/accounts", status_code=201)
    def create_account(body: AccountIn, caller: Caller) -> dict:
        require(caller, Role.ADMIN)
        account_id = ledger.create_account(body.holder_name, iban=body.iban, bic=body.bic)
        return _account_view(ledger.account_info(account_id))

    @app.post("/v1/accounts/{iban}/deposits", status_code=201)
    def deposit(iban: str, body: DepositIn, caller: Caller) -> dict:
        require(caller, Role.ADMIN)
        account = visible_account(caller, iban)
        result = ledger.transfer(
            idempotency_key=f"deposit:{body.idempotency_key}",
            debtor_id=funding,
            creditor_id=account.id,
            amount_cents=body.amount_cents,
            reference="fictional deposit",
            booked_at=clock(),
        )
        return {"transfer_id": result.transfer_id, "replayed": result.replayed}

    @app.get("/v1/accounts/{iban}")
    def read_account(iban: str, caller: Caller) -> dict:
        return _account_view(visible_account(caller, iban))

    @app.post("/v1/vop")
    def vop(body: VopIn, caller: Caller) -> dict:
        require(caller, Role.PSP)
        result = verify_payee(ledger, body.iban, body.name)
        return {"outcome": result.outcome.value, "suggested_name": result.suggested_name}

    @app.post("/v1/risk/assessments")
    def assess(body: AssessmentIn, caller: Caller) -> dict:
        debtor = own_account(caller, body.debtor_iban)
        result = scorer.assess(
            debtor_id=debtor.id,
            creditor_iban=body.creditor_iban,
            typed_name=body.name,
            amount_cents=body.amount_cents,
            at=clock(),
        )
        return {
            "decision": result.decision.value,
            "points": result.points,
            "reasons": [
                {"rule": r.rule, "points": r.points, "detail": r.detail} for r in result.reasons
            ],
            "vop": result.vop.outcome.value,
            "elapsed_ms": round(result.elapsed_ns / 1e6, 3),
        }

    @app.post("/v1/payments")
    async def send_payment(request: Request, caller: Caller) -> Response:
        require(caller, Role.PSP)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/xml":
            raise HTTPException(415, "Send the pacs.008 message as application/xml")
        message = pacs008.parse(await request.body())
        if not caller.acts_for(message.debtor.bic):
            raise HTTPException(403, "A bank can only send payments as the debtor agent")
        result = processor.process_transfer(message)
        return Response(result.report_xml, media_type="application/xml")

    @app.get("/v1/payments/{uetr}/trace")
    def trace(
        uetr: str,
        caller: Caller,
        attribution: Annotated[Attribution, Query()],
        max_depth: Annotated[int, Query(ge=0, le=MAX_TRACE_DEPTH)] = DEFAULT_MAX_DEPTH,
    ) -> dict:
        parties = ledger.payment_accounts(uetr)
        if parties is None:
            raise HTTPException(404, "Payment not found")
        if caller.role is Role.ADMIN:
            raise HTTPException(403, "Not allowed for this role")
        if caller.role is Role.PSP and not any(caller.acts_for(p.bic) for p in parties):
            raise HTTPException(404, "Payment not found")
        result = trace_payment(ledger, uetr, attribution=attribution, max_depth=max_depth)
        full = caller.role is Role.AUDITOR

        def node_view(node) -> dict:
            view = {
                "bic": node.bic,
                "depth": node.depth,
                "traced_in_cents": node.traced_in_cents,
                "remaining_cents": node.remaining_cents,
                "secured_cents": node.secured_cents,
                "moved_on_cents": node.moved_on_cents,
                "unfollowed_cents": node.unfollowed_cents,
            }
            if full or caller.acts_for(node.bic):
                view["iban"] = ledger.account_info(node.account_id).iban
            return view  # other banks' customers stay hidden: only their bank and amounts

        return {
            "uetr": uetr,
            "attribution": attribution.value,
            "payment_cents": result.payment_cents,
            "remaining_cents": result.remaining_cents,
            "secured_cents": result.secured_cents,
            "unfollowed_cents": result.unfollowed_cents,
            "accounts": [node_view(node) for node in result.nodes()],
        }

    @app.post("/v1/payments/{uetr}/freeze")
    def freeze(uetr: str, body: FreezeIn, caller: Caller) -> dict:
        require(caller, Role.PSP)
        if ledger.payment_accounts(uetr) is None:
            raise HTTPException(404, "Payment not found")
        result = freeze_traced_funds(
            ledger,
            uetr,
            attribution=body.attribution,
            reason=body.reason,
            placed_by=caller.client_id,
            max_depth=body.max_depth,
            only_bic=caller.bic,
        )
        return {
            "requested_cents": result.requested_cents,
            "secured_cents": result.secured_cents,
            "holds": [_hold_view(hold) for hold in result.holds],
        }

    @app.post("/v1/accounts/{iban}/holds", status_code=201)
    def place_hold(iban: str, body: HoldIn, caller: Caller) -> dict:
        account = own_account(caller, iban)
        if body.mode == "available":
            hold = ledger.freeze_available(
                account.id,
                body.amount_cents,
                reason=body.reason,
                placed_by=caller.client_id,
                uetr=body.uetr,
            )
            if hold is None:
                raise HTTPException(409, "Nothing spendable to hold")
        else:
            hold = ledger.place_hold(
                account.id,
                body.amount_cents,
                reason=body.reason,
                placed_by=caller.client_id,
                uetr=body.uetr,
            )
        return _hold_view(hold)

    @app.get("/v1/accounts/{iban}/holds")
    def list_holds(iban: str, caller: Caller) -> list[dict]:
        if caller.role is Role.ADMIN:
            raise HTTPException(403, "Not allowed for this role")
        account = visible_account(caller, iban)
        return [_hold_view(hold) for hold in ledger.active_holds(account.id)]

    @app.post("/v1/holds/{hold_id}/release")
    def release(hold_id: str, body: ReleaseIn, caller: Caller) -> dict:
        require(caller, Role.PSP)
        status = ledger.hold_status(hold_id)
        if status is None or not caller.acts_for(ledger.account_info(status[0].account_id).bic):
            raise HTTPException(404, "Hold not found")
        if status[1]:
            raise HTTPException(409, "Hold already released")
        ledger.release_hold(hold_id, reason=body.reason, released_by=caller.client_id)
        return {"id": hold_id, "released": True}

    @app.get("/v1/audit/rejections")
    def rejections(
        caller: Caller, reason_code: Annotated[str | None, Query(max_length=4)] = None
    ) -> list[dict]:
        require(caller, Role.AUDITOR, Role.PSP)
        records = audit.rejections(reason_code=reason_code)
        if caller.role is Role.PSP:
            records = [r for r in records if caller.bic in (r.debtor_bic, r.creditor_bic)]
        return [
            {
                "recorded_at": r.recorded_at.isoformat(),
                "msg_id": r.msg_id,
                "tx_id": r.tx_id,
                "uetr": r.uetr,
                "debtor_bic": r.debtor_bic,
                "debtor_iban": r.debtor_iban,
                "creditor_bic": r.creditor_bic,
                "creditor_iban": r.creditor_iban,
                "amount_cents": r.amount_cents,
                "reason_code": r.reason_code,
            }
            for r in records
        ]

    @app.get("/v1/invariants")
    def invariants(caller: Caller) -> dict:
        require(caller, Role.AUDITOR, Role.ADMIN)
        report = ledger.verify_invariants()
        return {"ok": report.ok, "problems": report.problems}

    app.add_middleware(BodyLimit, max_bytes=max_body_bytes)
    app.add_middleware(SecurityHeaders)  # added last, so it is the outermost
    return app


def create_app_from_env() -> FastAPI:
    """Factory for uvicorn. Refuses to start without a safe key file (fail closed)."""
    url = os.environ.get("ECHTZEIT_DATABASE_URL", "sqlite:///echtzeit.db")
    keys_file = os.environ.get("ECHTZEIT_API_KEYS_FILE")
    if not keys_file:
        raise RuntimeError("set ECHTZEIT_API_KEYS_FILE to a key file (see echtzeit.api.auth)")
    keys = KeyStore.from_file(keys_file)
    engine = make_engine(url)
    init_db(engine)
    sessions = make_session_factory(engine)
    return create_app(Ledger(sessions), AuditLog(sessions), keys)
