import os
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from echtzeit.errors import MessageError
from echtzeit.iso20022 import pacs002, pacs008
from echtzeit.iso20022.identifiers import (
    FICTIONAL_BIC,
    bic_is_valid,
    iban_is_valid,
    make_german_iban,
    new_uetr,
    uetr_is_valid,
)

T0 = datetime(2026, 11, 25, 10, 15, 30, 123000, tzinfo=UTC)
T1 = datetime(2026, 11, 25, 10, 15, 30, 456000, tzinfo=UTC)


@pytest.fixture
def transfer():
    return pacs008.CreditTransfer(
        msg_id="MSG-0001",
        created_at=T0,
        end_to_end_id="E2E-0001",
        tx_id="TX-0001",
        uetr=new_uetr(),
        amount_cents=1_050,
        settlement_date=date(2026, 11, 25),
        accepted_at=T0,
        debtor=pacs008.Party("Alice Example", make_german_iban("1"), FICTIONAL_BIC),
        creditor=pacs008.Party("Bob Example", make_german_iban("2"), "ECHTDEF1"),
        remittance="Invoice 42",
    )


def _tamper(xml: bytes, old: str, new: str) -> bytes:
    text = xml.decode("utf-8")
    assert old in text, f"{old!r} not found in the message"
    return text.replace(old, new, 1).encode("utf-8")


# -- identifiers ------------------------------------------------------------


def test_known_real_world_example_iban_is_valid():
    # Widely published example IBAN used in documentation, not a real customer account.
    assert iban_is_valid("DE89370400440532013000")


def test_generated_iban_is_valid_and_has_german_length():
    iban = make_german_iban("123456")
    assert len(iban) == 22
    assert iban.startswith("DE")
    assert iban_is_valid(iban)


@pytest.mark.parametrize(
    "iban",
    ["DE89370400440532013001", "de89370400440532013000", "DE89 3704 0044 0532 0130 00", "", None],
)
def test_bad_ibans_are_invalid(iban):
    assert not iban_is_valid(iban)


def test_bic_and_uetr_validation():
    assert bic_is_valid("ECHTDEFF")
    assert bic_is_valid("ECHTDEFFXXX")
    assert not bic_is_valid("echtdeff")
    assert not bic_is_valid("ECHTDE")
    assert uetr_is_valid(new_uetr())
    assert not uetr_is_valid("not-a-uuid")
    assert not uetr_is_valid(new_uetr().upper())


# -- pacs.008 ---------------------------------------------------------------


def test_pacs008_round_trip(transfer):
    assert pacs008.parse(pacs008.build(transfer)) == transfer


def test_pacs008_round_trip_without_remittance(transfer):
    bare = replace(transfer, remittance=None)
    assert pacs008.parse(pacs008.build(bare)) == bare


def test_pacs008_amount_is_written_with_two_decimals(transfer):
    xml = pacs008.build(replace(transfer, amount_cents=5)).decode()
    assert '<IntrBkSttlmAmt Ccy="EUR">0.05</IntrBkSttlmAmt>' in xml
    assert '<TtlIntrBkSttlmAmt Ccy="EUR">0.05</TtlIntrBkSttlmAmt>' in xml


def test_pacs008_carries_the_sepa_instant_markers(transfer):
    xml = pacs008.build(transfer).decode()
    assert "<Cd>SEPA</Cd>" in xml
    assert "<Cd>INST</Cd>" in xml
    assert "<ChrgBr>SLEV</ChrgBr>" in xml
    assert "<NbOfTxs>1</NbOfTxs>" in xml
    assert "2026-11-25T10:15:30.123Z" in xml


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"amount_cents": 0}, "amount"),
        ({"amount_cents": pacs008.MAX_AMOUNT_CENTS + 1}, "amount"),
        ({"amount_cents": 1.5}, "integer"),
        ({"msg_id": ""}, "msg_id"),
        ({"end_to_end_id": "x" * 36}, "end_to_end_id"),
        ({"uetr": "123"}, "uetr"),
        ({"created_at": datetime(2026, 1, 1)}, "time zone"),
        ({"remittance": "x" * 141}, "remittance"),
    ],
)
def test_pacs008_build_rejects_rule_violations(transfer, change, fragment):
    with pytest.raises(MessageError) as caught:
        pacs008.build(replace(transfer, **change))
    assert fragment in str(caught.value)


def test_pacs008_build_rejects_bad_party_data(transfer):
    bad = replace(
        transfer,
        debtor=pacs008.Party("x" * 71, "DE00", "bad"),
        creditor=replace(transfer.creditor, iban=transfer.creditor.iban[:-1] + "9"),
    )
    with pytest.raises(MessageError) as caught:
        pacs008.build(bad)
    problems = caught.value.problems
    assert any("debtor name" in p for p in problems)
    assert any("debtor IBAN" in p for p in problems)
    assert any("debtor BIC" in p for p in problems)
    assert any("creditor IBAN" in p for p in problems)


def test_pacs008_build_rejects_same_debtor_and_creditor_iban(transfer):
    same = replace(transfer, creditor=replace(transfer.creditor, iban=transfer.debtor.iban))
    with pytest.raises(MessageError, match="must differ"):
        pacs008.build(same)


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("<NbOfTxs>1</NbOfTxs>", "<NbOfTxs>2</NbOfTxs>", "NbOfTxs"),
        ('Ccy="EUR">10.50</IntrBkSttlmAmt>', 'Ccy="USD">10.50</IntrBkSttlmAmt>', "EUR"),
        ("<ChrgBr>SLEV</ChrgBr>", "<ChrgBr>SHAR</ChrgBr>", "ChrgBr"),
        ("<Cd>INST</Cd>", "<Cd>STND</Cd>", "LclInstrm"),
        ("<Cd>SEPA</Cd>", "<Cd>URGP</Cd>", "SvcLvl"),
        (
            '<IntrBkSttlmAmt Ccy="EUR">10.50</IntrBkSttlmAmt>',
            '<IntrBkSttlmAmt Ccy="EUR">10.505</IntrBkSttlmAmt>',
            "fraction",
        ),
        (
            '<IntrBkSttlmAmt Ccy="EUR">10.50</IntrBkSttlmAmt>',
            '<IntrBkSttlmAmt Ccy="EUR">11.00</IntrBkSttlmAmt>',
            "must equal",
        ),
        (
            "2026-11-25T10:15:30.123Z</AccptncDtTm>",
            "2026-11-25T10:15:30.123</AccptncDtTm>",
            "time zone",
        ),
    ],
)
def test_pacs008_parse_reports_the_violation(transfer, old, new, fragment):
    xml = pacs008.build(transfer)
    with pytest.raises(MessageError) as caught:
        pacs008.parse(_tamper(xml, old, new))
    assert fragment in str(caught.value)


def test_pacs008_parse_reports_missing_mandatory_elements(transfer):
    xml = pacs008.build(transfer)
    xml = _tamper(xml, f"<UETR>{transfer.uetr}</UETR>", "")
    with pytest.raises(MessageError, match="UETR"):
        pacs008.parse(xml)


def test_pacs008_parse_rejects_wrong_namespace(transfer):
    xml = pacs008.build(transfer)
    wrong = _tamper(xml, pacs008.NAMESPACE, "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.99")
    with pytest.raises(MessageError, match="namespace"):
        pacs008.parse(wrong)


def test_pacs008_parse_rejects_two_transactions(transfer):
    xml = pacs008.build(transfer).decode()
    start = xml.index("<CdtTrfTxInf>")
    end = xml.index("</CdtTrfTxInf>") + len("</CdtTrfTxInf>")
    doubled = xml[:end] + xml[start:end] + xml[end:]
    with pytest.raises(MessageError, match="exactly one"):
        pacs008.parse(doubled)


@pytest.mark.parametrize("garbage", [b"", b"not xml", b"<a><b></a>"])
def test_pacs008_parse_rejects_malformed_xml(garbage):
    with pytest.raises(MessageError):
        pacs008.parse(garbage)


def test_pacs008_parse_blocks_entity_expansion_and_external_entities(transfer):
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">&lol2;</Document>"""
    with pytest.raises(MessageError, match="unsafe"):
        pacs008.parse(bomb)
    external = b"""<?xml version="1.0"?>
<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">&x;</Document>"""
    with pytest.raises(MessageError, match="unsafe"):
        pacs008.parse(external)


# -- pacs.002 ---------------------------------------------------------------


def _report(transfer, reason_code=None):
    return pacs002.report_for(
        transfer, msg_id="RPT-0001", status_id="STS-0001", created_at=T1, reason_code=reason_code
    )


def test_pacs002_accepted_round_trip(transfer):
    report = _report(transfer)
    assert report.status == pacs002.ACCEPTED
    assert pacs002.parse(pacs002.build(report)) == report


@pytest.mark.parametrize("code", ["AC04", "AB05", "MS03", "RR04", "CNOR"])
def test_pacs002_rejected_round_trip(transfer, code):
    report = _report(transfer, code)
    assert report.status == pacs002.REJECTED
    assert pacs002.parse(pacs002.build(report)) == report


def test_pacs002_points_back_to_the_original_payment(transfer):
    report = _report(transfer)
    assert report.original_msg_id == transfer.msg_id
    assert report.original_end_to_end_id == transfer.end_to_end_id
    assert report.original_tx_id == transfer.tx_id
    assert report.original_uetr == transfer.uetr
    xml = pacs002.build(report).decode()
    assert "<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>" in xml


@pytest.mark.parametrize("code", ["TM01", "XXXX", "AM04", ""])
def test_pacs002_rejects_reason_codes_the_guidelines_do_not_allow(transfer, code):
    with pytest.raises(MessageError, match="not allowed"):
        pacs002.build(_report(transfer, code))


def test_pacs002_accepted_report_cannot_carry_a_reason(transfer):
    report = replace(_report(transfer), reason_code="AC04")
    with pytest.raises(MessageError, match="must not carry"):
        pacs002.build(report)


def test_pacs002_parse_rejects_conflicting_status(transfer):
    xml = pacs002.build(_report(transfer, "AC04"))
    both = _tamper(
        xml,
        "<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>",
        "<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId><GrpSts>ACCP</GrpSts>",
    )
    with pytest.raises(MessageError, match="status must be"):
        pacs002.parse(both)


def test_pacs002_parse_rejects_a_reply_to_something_other_than_pacs008(transfer):
    xml = pacs002.build(_report(transfer))
    wrong = _tamper(xml, "pacs.008.001.08</OrgnlMsgNmId>", "pain.001.001.09</OrgnlMsgNmId>")
    with pytest.raises(MessageError, match="pacs.008"):
        pacs002.parse(wrong)


# -- validation against the official ISO 20022 schemas -----------------------
# The schemas are not stored in this repository. Point ECHTZEIT_XSD_DIR at a folder
# holding pacs.008.001.08.xsd and pacs.002.001.10.xsd to run these tests.


def _schema(name: str):
    folder = os.environ.get("ECHTZEIT_XSD_DIR")
    if not folder or not (Path(folder) / name).is_file():
        pytest.skip(f"{name} not available (set ECHTZEIT_XSD_DIR)")
    etree = pytest.importorskip("lxml.etree")
    return etree, etree.XMLSchema(etree.parse(str(Path(folder) / name)))


def _assert_valid(etree, schema, xml: bytes):
    document = etree.fromstring(xml)
    assert schema.validate(document), schema.error_log


def test_pacs008_output_validates_against_the_iso_schema(transfer):
    etree, schema = _schema("pacs.008.001.08.xsd")
    _assert_valid(etree, schema, pacs008.build(transfer))
    _assert_valid(etree, schema, pacs008.build(replace(transfer, remittance=None)))


def test_pacs008_schema_check_can_fail(transfer):
    etree, schema = _schema("pacs.008.001.08.xsd")
    broken = _tamper(pacs008.build(transfer), "<ChrgBr>SLEV</ChrgBr>", "")
    assert not schema.validate(etree.fromstring(broken))


def test_pacs002_output_validates_against_the_iso_schema(transfer):
    etree, schema = _schema("pacs.002.001.10.xsd")
    _assert_valid(etree, schema, pacs002.build(_report(transfer)))
    _assert_valid(etree, schema, pacs002.build(_report(transfer, "AC04")))


def test_pacs002_schema_check_can_fail(transfer):
    etree, schema = _schema("pacs.002.001.10.xsd")
    broken = _tamper(
        pacs002.build(_report(transfer)),
        "<StsId>STS-0001</StsId>",
        "<StsId>STS-0001</StsId><Bogus/>",
    )
    assert not schema.validate(etree.fromstring(broken))


@pytest.mark.parametrize(
    "amount",
    ["1E999990", "1E1000000", "1E2", "1_0.00", "\uff11.\uff10\uff10", "+5.00", "5.", ".5", "-1.00"],
)
def test_pacs008_parse_accepts_only_plain_decimal_amounts(transfer, amount):
    """Exponents, signs, underscores and non-ASCII digits are refused quickly as MessageError."""
    xml = pacs008.build(transfer)
    for tag in ("IntrBkSttlmAmt", "TtlIntrBkSttlmAmt"):
        xml = _tamper(xml, f'<{tag} Ccy="EUR">10.50</{tag}>', f'<{tag} Ccy="EUR">{amount}</{tag}>')
    started = time.perf_counter()
    with pytest.raises(MessageError) as caught:
        pacs008.parse(xml)
    assert time.perf_counter() - started < 1.0
    assert amount not in str(caught.value)  # the submitted value is not echoed
