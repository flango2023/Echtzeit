"""pacs.008.001.08: FI to FI Customer Credit Transfer, SEPA Instant subset.

Source for the rules: EPC SCT Inst Inter-PSP Implementation Guidelines 2025 v1.0 (EPC122-16),
which uses ISO 20022 version pacs.008.001.08. The subset covers one credit transfer between
two agents identified by BIC, accounts identified by IBAN, EUR only, charge bearer SLEV,
service level SEPA and local instrument INST.

Not covered: postal addresses, structured remittance information, party identification
(organisation or private ID), purpose codes, category purpose, reimbursement agents and
intermediary agents. The EPC character-set rule is not enforced.
"""

# ElementTree only builds documents here. Parsing of input goes through defusedxml.
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from datetime import date, datetime

from echtzeit.errors import MessageError
from echtzeit.iso20022 import _xml
from echtzeit.iso20022.identifiers import bic_is_valid, iban_is_valid, uetr_is_valid

NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"
MESSAGE_NAME = "pacs.008.001.08"

MAX_AMOUNT_CENTS = 99_999_999_999  # 999,999,999.99 EUR, EPC limit for the transaction amount
MAX_NAME_LENGTH = 70
MAX_REFERENCE_LENGTH = 35
MAX_REMITTANCE_LENGTH = 140


@dataclass(frozen=True)
class Party:
    """A customer, with the IBAN of the account and the BIC of the account-holding agent."""

    name: str
    iban: str
    bic: str


@dataclass(frozen=True)
class CreditTransfer:
    msg_id: str
    created_at: datetime
    end_to_end_id: str
    tx_id: str
    uetr: str
    amount_cents: int
    settlement_date: date
    accepted_at: datetime
    debtor: Party
    creditor: Party
    remittance: str | None = None


def validate(transfer: CreditTransfer) -> list[str]:
    """Return every rule the transfer breaks. An empty list means it is acceptable."""
    problems: list[str] = []

    def check_reference(label: str, value: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_REFERENCE_LENGTH:
            problems.append(f"{label} must have 1 to {MAX_REFERENCE_LENGTH} characters")

    check_reference("msg_id", transfer.msg_id)
    check_reference("end_to_end_id", transfer.end_to_end_id)
    check_reference("tx_id", transfer.tx_id)

    if not uetr_is_valid(transfer.uetr):
        problems.append("uetr must be a lower-case UUID version 4")

    amount = transfer.amount_cents
    if isinstance(amount, bool) or not isinstance(amount, int):
        problems.append("amount_cents must be an integer")
    elif not 1 <= amount <= MAX_AMOUNT_CENTS:
        problems.append("amount must be between 0.01 and 999999999.99 EUR")

    for label, moment in (
        ("created_at", transfer.created_at),
        ("accepted_at", transfer.accepted_at),
    ):
        if moment.tzinfo is None:
            problems.append(f"{label} must include a time zone")

    for role, party in (("debtor", transfer.debtor), ("creditor", transfer.creditor)):
        if not party.name or not party.name.strip() or len(party.name) > MAX_NAME_LENGTH:
            problems.append(f"{role} name must have 1 to {MAX_NAME_LENGTH} characters")
        if not iban_is_valid(party.iban):
            problems.append(f"{role} IBAN is not valid")
        if not bic_is_valid(party.bic):
            problems.append(f"{role} BIC is not valid")

    if transfer.debtor.iban == transfer.creditor.iban:
        problems.append("debtor and creditor IBAN must differ")

    remittance = transfer.remittance
    if remittance is not None and not 1 <= len(remittance) <= MAX_REMITTANCE_LENGTH:
        problems.append(f"remittance must have 1 to {MAX_REMITTANCE_LENGTH} characters")

    return problems


def build(transfer: CreditTransfer) -> bytes:
    """Serialize a credit transfer. Raises MessageError if it breaks a rule."""
    problems = validate(transfer)
    if problems:
        raise MessageError(problems)

    ns = NAMESPACE
    amount = _xml.cents_to_amount(transfer.amount_cents)
    root = ET.Element("Document", {"xmlns": ns})
    message = _xml.add(root, "FIToFICstmrCdtTrf")

    header = _xml.add(message, "GrpHdr")
    _xml.add(header, "MsgId", transfer.msg_id)
    _xml.add(header, "CreDtTm", _xml.format_datetime(transfer.created_at))
    _xml.add(header, "NbOfTxs", "1")
    _xml.add(header, "TtlIntrBkSttlmAmt", amount, Ccy="EUR")
    _xml.add(header, "IntrBkSttlmDt", _xml.format_date(transfer.settlement_date))
    settlement = _xml.add(header, "SttlmInf")
    _xml.add(settlement, "SttlmMtd", "CLRG")
    payment_type = _xml.add(header, "PmtTpInf")
    _xml.add(_xml.add(payment_type, "SvcLvl"), "Cd", "SEPA")
    _xml.add(_xml.add(payment_type, "LclInstrm"), "Cd", "INST")
    _add_agent(header, "InstgAgt", transfer.debtor.bic)
    _add_agent(header, "InstdAgt", transfer.creditor.bic)

    tx = _xml.add(message, "CdtTrfTxInf")
    payment_id = _xml.add(tx, "PmtId")
    _xml.add(payment_id, "EndToEndId", transfer.end_to_end_id)
    _xml.add(payment_id, "TxId", transfer.tx_id)
    _xml.add(payment_id, "UETR", transfer.uetr)
    _xml.add(tx, "IntrBkSttlmAmt", amount, Ccy="EUR")
    _xml.add(tx, "AccptncDtTm", _xml.format_datetime(transfer.accepted_at))
    _xml.add(tx, "ChrgBr", "SLEV")
    # Element order is fixed by the XSD: Dbtr, DbtrAcct, DbtrAgt, CdtrAgt, Cdtr, CdtrAcct.
    _add_name(tx, "Dbtr", transfer.debtor.name)
    _add_account(tx, "DbtrAcct", transfer.debtor.iban)
    _add_agent(tx, "DbtrAgt", transfer.debtor.bic)
    _add_agent(tx, "CdtrAgt", transfer.creditor.bic)
    _add_name(tx, "Cdtr", transfer.creditor.name)
    _add_account(tx, "CdtrAcct", transfer.creditor.iban)
    if transfer.remittance is not None:
        _xml.add(_xml.add(tx, "RmtInf"), "Ustrd", transfer.remittance)

    return _xml.serialize(root)


def _add_agent(parent: ET.Element, tag: str, bic: str) -> None:
    _xml.add(_xml.add(_xml.add(parent, tag), "FinInstnId"), "BICFI", bic)


def _add_name(parent: ET.Element, tag: str, name: str) -> None:
    _xml.add(_xml.add(parent, tag), "Nm", name)


def _add_account(parent: ET.Element, tag: str, iban: str) -> None:
    _xml.add(_xml.add(_xml.add(parent, tag), "Id"), "IBAN", iban)


def parse(xml: bytes | str) -> CreditTransfer:
    """Read a pacs.008 message. Raises MessageError listing every problem found."""
    root = _xml.parse_root(xml)
    ns = NAMESPACE
    if root.tag != f"{{{ns}}}Document":
        raise MessageError([f"root element must be Document in namespace {ns}"])

    problems: list[str] = []

    def need(path: str) -> str:
        value = _xml.text_of(root, path, ns)
        if value is None:
            problems.append(f"missing mandatory element {path.replace('p:', '')}")
            return ""
        return value

    def expect(path: str, allowed: str) -> None:
        value = _xml.text_of(root, path, ns)
        if value != allowed:
            problems.append(f"{path.replace('p:', '')} must be {allowed!r}")

    base = "p:FIToFICstmrCdtTrf/"
    hdr = base + "p:GrpHdr/"
    tx = base + "p:CdtTrfTxInf/"

    msg_id = need(hdr + "p:MsgId")
    created_raw = need(hdr + "p:CreDtTm")
    expect(hdr + "p:NbOfTxs", "1")
    method = _xml.text_of(root, hdr + "p:SttlmInf/p:SttlmMtd", ns)
    if method not in ("CLRG", "INGA", "INDA"):
        problems.append("SttlmMtd must be CLRG, INGA or INDA")
    expect(hdr + "p:PmtTpInf/p:SvcLvl/p:Cd", "SEPA")
    expect(hdr + "p:PmtTpInf/p:LclInstrm/p:Cd", "INST")
    settlement_raw = need(hdr + "p:IntrBkSttlmDt")
    total_raw = need(hdr + "p:TtlIntrBkSttlmAmt")

    if len(root.findall(f".//{{{ns}}}CdtTrfTxInf")) != 1:
        problems.append("exactly one CdtTrfTxInf is required")

    end_to_end = need(tx + "p:PmtId/p:EndToEndId")
    tx_id = need(tx + "p:PmtId/p:TxId")
    uetr = need(tx + "p:PmtId/p:UETR")
    amount_raw = need(tx + "p:IntrBkSttlmAmt")
    accepted_raw = need(tx + "p:AccptncDtTm")
    expect(tx + "p:ChrgBr", "SLEV")
    debtor_name = need(tx + "p:Dbtr/p:Nm")
    debtor_iban = need(tx + "p:DbtrAcct/p:Id/p:IBAN")
    debtor_bic = need(tx + "p:DbtrAgt/p:FinInstnId/p:BICFI")
    creditor_name = need(tx + "p:Cdtr/p:Nm")
    creditor_iban = need(tx + "p:CdtrAcct/p:Id/p:IBAN")
    creditor_bic = need(tx + "p:CdtrAgt/p:FinInstnId/p:BICFI")
    remittance = _xml.text_of(root, tx + "p:RmtInf/p:Ustrd", ns)

    for path in (hdr + "p:TtlIntrBkSttlmAmt", tx + "p:IntrBkSttlmAmt"):
        element = root.find(path, {"p": ns})
        if element is not None and element.get("Ccy") != "EUR":
            problems.append(f"{path.replace('p:', '')} must be in EUR")

    amount_cents = _convert(problems, "IntrBkSttlmAmt", amount_raw, _xml.amount_to_cents)
    total_cents = _convert(problems, "TtlIntrBkSttlmAmt", total_raw, _xml.amount_to_cents)
    if amount_cents is not None and total_cents is not None and amount_cents != total_cents:
        problems.append("TtlIntrBkSttlmAmt must equal IntrBkSttlmAmt")
    created_at = _convert(problems, "CreDtTm", created_raw, _xml.parse_datetime)
    accepted_at = _convert(problems, "AccptncDtTm", accepted_raw, _xml.parse_datetime)
    settlement_date = _convert(problems, "IntrBkSttlmDt", settlement_raw, date.fromisoformat)

    if problems:
        raise MessageError(problems)

    transfer = CreditTransfer(
        msg_id=msg_id,
        created_at=created_at,
        end_to_end_id=end_to_end,
        tx_id=tx_id,
        uetr=uetr,
        amount_cents=amount_cents,
        settlement_date=settlement_date,
        accepted_at=accepted_at,
        debtor=Party(debtor_name, debtor_iban, debtor_bic),
        creditor=Party(creditor_name, creditor_iban, creditor_bic),
        remittance=remittance,
    )
    rule_problems = validate(transfer)
    if rule_problems:
        raise MessageError(rule_problems)
    return transfer


def _convert(problems: list[str], label: str, raw: str, convert):
    if not raw:
        return None
    try:
        return convert(raw)
    except ValueError as exc:
        problems.append(f"{label}: {exc}")
        return None


__all__ = ["MESSAGE_NAME", "NAMESPACE", "CreditTransfer", "Party", "build", "parse", "validate"]
