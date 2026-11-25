"""pacs.002.001.10: FI to FI Payment Status Report, SEPA Instant subset.

Source for the rules: EPC SCT Inst Inter-PSP Implementation Guidelines 2025 v1.0 (EPC122-16),
sections 2.2 (negative confirmation) and 2.3 (positive confirmation).

* Positive confirmation: group status ACCP.
* Negative confirmation: transaction status RJCT with one reason code.

The report is sent by the beneficiary PSP to the originator PSP, so the reason code TM01
(only allowed towards the beneficiary PSP) is not accepted here. Only the reason codes the
guidelines list for a negative confirmation are accepted. They contain no code for "frozen
as suspected fraud", so a fraud freeze has to use a generic code such as RR04 or MS03. That
mapping is a modelling choice of this project and is not defined by the EPC.
"""

# ElementTree only builds documents here. Parsing of input goes through defusedxml.
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from datetime import datetime

from echtzeit.errors import MessageError
from echtzeit.iso20022 import _xml
from echtzeit.iso20022.identifiers import bic_is_valid, uetr_is_valid
from echtzeit.iso20022.pacs008 import MESSAGE_NAME as PACS008_NAME
from echtzeit.iso20022.pacs008 import CreditTransfer

NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10"
MESSAGE_NAME = "pacs.002.001.10"

ACCEPTED = "ACCP"
REJECTED = "RJCT"

# Reason codes listed in EPC122-16 section 2.2.2 for a negative confirmation to the originator PSP.
REJECTION_REASONS = frozenset(
    {
        "AB05", "AB06", "AB07", "AB08", "AB09", "AB10",
        "AC01", "AC04", "AC06", "AG01", "AG02", "AG09", "AG10", "AG11",
        "AM05", "AM23", "BE04", "MD07", "MS02", "MS03", "RC01",
        "RR01", "RR02", "RR03", "RR04", "DNOR", "CNOR",
    }
)  # fmt: skip


@dataclass(frozen=True)
class StatusReport:
    msg_id: str
    created_at: datetime
    status_id: str
    original_msg_id: str
    original_end_to_end_id: str
    original_tx_id: str
    original_uetr: str
    accepted_at: datetime
    status: str  # ACCEPTED or REJECTED
    reporting_bic: str  # BIC of the PSP that sends the report (the beneficiary PSP)
    debtor_agent_bic: str  # BIC of the originator PSP
    reason_code: str | None = None  # required when REJECTED, absent when ACCEPTED


def report_for(
    transfer: CreditTransfer,
    *,
    msg_id: str,
    status_id: str,
    created_at: datetime,
    reason_code: str | None = None,
) -> StatusReport:
    """Build the report that answers `transfer`: accepted if no reason code is given."""
    return StatusReport(
        msg_id=msg_id,
        created_at=created_at,
        status_id=status_id,
        original_msg_id=transfer.msg_id,
        original_end_to_end_id=transfer.end_to_end_id,
        original_tx_id=transfer.tx_id,
        original_uetr=transfer.uetr,
        accepted_at=transfer.accepted_at,
        status=ACCEPTED if reason_code is None else REJECTED,
        reporting_bic=transfer.creditor.bic,
        debtor_agent_bic=transfer.debtor.bic,
        reason_code=reason_code,
    )


def validate(report: StatusReport) -> list[str]:
    problems: list[str] = []
    for label, value in (
        ("msg_id", report.msg_id),
        ("status_id", report.status_id),
        ("original_msg_id", report.original_msg_id),
        ("original_end_to_end_id", report.original_end_to_end_id),
        ("original_tx_id", report.original_tx_id),
    ):
        if not isinstance(value, str) or not 1 <= len(value) <= 35:
            problems.append(f"{label} must have 1 to 35 characters")
    if not uetr_is_valid(report.original_uetr):
        problems.append("original_uetr must be a lower-case UUID version 4")
    for label, moment in (("created_at", report.created_at), ("accepted_at", report.accepted_at)):
        if moment.tzinfo is None:
            problems.append(f"{label} must include a time zone")
    for label, bic in (
        ("reporting_bic", report.reporting_bic),
        ("debtor_agent_bic", report.debtor_agent_bic),
    ):
        if not bic_is_valid(bic):
            problems.append(f"{label} is not valid")
    if report.status == ACCEPTED:
        if report.reason_code is not None:
            problems.append("an accepted report must not carry a reason code")
    elif report.status == REJECTED:
        if report.reason_code not in REJECTION_REASONS:
            problems.append(
                f"reason_code {report.reason_code!r} is not allowed in a negative confirmation"
            )
    else:
        problems.append(f"status must be {ACCEPTED} or {REJECTED}")
    return problems


def build(report: StatusReport) -> bytes:
    problems = validate(report)
    if problems:
        raise MessageError(problems)

    ns = NAMESPACE
    root = ET.Element("Document", {"xmlns": ns})
    message = _xml.add(root, "FIToFIPmtStsRpt")

    header = _xml.add(message, "GrpHdr")
    _xml.add(header, "MsgId", report.msg_id)
    _xml.add(header, "CreDtTm", _xml.format_datetime(report.created_at))
    _add_agent(header, "InstgAgt", report.reporting_bic)
    _add_agent(header, "InstdAgt", report.debtor_agent_bic)

    group = _xml.add(message, "OrgnlGrpInfAndSts")
    _xml.add(group, "OrgnlMsgId", report.original_msg_id)
    _xml.add(group, "OrgnlMsgNmId", PACS008_NAME)
    if report.status == ACCEPTED:
        _xml.add(group, "GrpSts", ACCEPTED)

    tx = _xml.add(message, "TxInfAndSts")
    _xml.add(tx, "StsId", report.status_id)
    _xml.add(tx, "OrgnlEndToEndId", report.original_end_to_end_id)
    _xml.add(tx, "OrgnlTxId", report.original_tx_id)
    _xml.add(tx, "OrgnlUETR", report.original_uetr)
    if report.status == REJECTED:
        _xml.add(tx, "TxSts", REJECTED)
        reason = _xml.add(tx, "StsRsnInf")
        originator = _xml.add(reason, "Orgtr")
        _xml.add(_xml.add(_xml.add(originator, "Id"), "OrgId"), "AnyBIC", report.reporting_bic)
        _xml.add(_xml.add(reason, "Rsn"), "Cd", report.reason_code)
    _xml.add(tx, "AccptncDtTm", _xml.format_datetime(report.accepted_at))
    reference = _xml.add(tx, "OrgnlTxRef")
    payment_type = _xml.add(reference, "PmtTpInf")
    _xml.add(_xml.add(payment_type, "SvcLvl"), "Cd", "SEPA")
    _xml.add(_xml.add(payment_type, "LclInstrm"), "Cd", "INST")
    _add_agent(reference, "DbtrAgt", report.debtor_agent_bic)

    return _xml.serialize(root)


def _add_agent(parent: ET.Element, tag: str, bic: str) -> None:
    _xml.add(_xml.add(_xml.add(parent, tag), "FinInstnId"), "BICFI", bic)


def parse(xml: bytes | str) -> StatusReport:
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

    base = "p:FIToFIPmtStsRpt/"
    hdr = base + "p:GrpHdr/"
    grp = base + "p:OrgnlGrpInfAndSts/"
    tx = base + "p:TxInfAndSts/"

    msg_id = need(hdr + "p:MsgId")
    created_raw = need(hdr + "p:CreDtTm")
    reporting_bic = need(hdr + "p:InstgAgt/p:FinInstnId/p:BICFI")
    original_msg_id = need(grp + "p:OrgnlMsgId")
    original_name = need(grp + "p:OrgnlMsgNmId")
    if original_name and not original_name.startswith("pacs.008"):
        problems.append("OrgnlMsgNmId must begin with 'pacs.008'")
    status_id = need(tx + "p:StsId")
    end_to_end = need(tx + "p:OrgnlEndToEndId")
    tx_id = need(tx + "p:OrgnlTxId")
    uetr = need(tx + "p:OrgnlUETR")
    accepted_raw = need(tx + "p:AccptncDtTm")
    debtor_bic = need(tx + "p:OrgnlTxRef/p:DbtrAgt/p:FinInstnId/p:BICFI")
    if _xml.text_of(root, tx + "p:OrgnlTxRef/p:PmtTpInf/p:LclInstrm/p:Cd", ns) != "INST":
        problems.append("OrgnlTxRef/PmtTpInf/LclInstrm/Cd must be 'INST'")

    group_status = _xml.text_of(root, grp + "p:GrpSts", ns)
    tx_status = _xml.text_of(root, tx + "p:TxSts", ns)
    reason_code = _xml.text_of(root, tx + "p:StsRsnInf/p:Rsn/p:Cd", ns)
    if group_status == ACCEPTED and tx_status is None:
        status = ACCEPTED
    elif tx_status == REJECTED and group_status is None:
        status = REJECTED
        if reason_code is None:
            problems.append("a rejected report needs StsRsnInf/Rsn/Cd")
    else:
        status = ""
        problems.append(
            f"status must be GrpSts {ACCEPTED} or TxSts {REJECTED}, found GrpSts={group_status!r} TxSts={tx_status!r}"
        )

    created_at = _convert(problems, "CreDtTm", created_raw)
    accepted_at = _convert(problems, "AccptncDtTm", accepted_raw)
    if problems:
        raise MessageError(problems)

    report = StatusReport(
        msg_id=msg_id,
        created_at=created_at,
        status_id=status_id,
        original_msg_id=original_msg_id,
        original_end_to_end_id=end_to_end,
        original_tx_id=tx_id,
        original_uetr=uetr,
        accepted_at=accepted_at,
        status=status,
        reporting_bic=reporting_bic,
        debtor_agent_bic=debtor_bic,
        reason_code=reason_code if status == REJECTED else None,
    )
    rule_problems = validate(report)
    if rule_problems:
        raise MessageError(rule_problems)
    return report


def _convert(problems: list[str], label: str, raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return _xml.parse_datetime(raw)
    except ValueError as exc:
        problems.append(f"{label}: {exc}")
        return None


__all__ = [
    "ACCEPTED",
    "MESSAGE_NAME",
    "NAMESPACE",
    "REJECTED",
    "REJECTION_REASONS",
    "StatusReport",
    "build",
    "parse",
    "report_for",
    "validate",
]
