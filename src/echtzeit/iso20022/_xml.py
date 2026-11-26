"""Small XML helpers shared by the message modules."""

# ElementTree only builds documents here. Parsing of input goes through defusedxml.
import re
import xml.etree.ElementTree as ET  # nosec B405
from datetime import UTC, date, datetime

from defusedxml import ElementTree as SafeET

from echtzeit.errors import MessageError


def add(parent: ET.Element, name: str, text: str | None = None, **attrs: str) -> ET.Element:
    """Add a child element.

    Tags are written without a namespace. The caller sets `xmlns` on the root element, so
    every unprefixed element belongs to the default namespace. ElementTree cannot combine
    a default namespace with unqualified attributes such as `Ccy`, so this avoids its
    namespace handling and, with it, any global state.
    """
    element = ET.SubElement(parent, name, attrs)
    if text is not None:
        element.text = text
    return element


def serialize(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parse_root(xml: bytes | str) -> ET.Element:
    """Parse untrusted XML with defusedxml (blocks entity-expansion and external entities)."""
    try:
        return SafeET.fromstring(xml)
    except Exception as exc:  # defusedxml raises several types
        raise MessageError([f"not well-formed or unsafe XML: {exc}"]) from exc


def text_of(parent: ET.Element, path: str, ns: str) -> str | None:
    element = parent.find(path, {"p": ns})
    if element is None or element.text is None or not element.text.strip():
        return None
    return element.text.strip()


def cents_to_amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


# ASCII digits, an optional point and one or two fraction digits. Stricter than xs:decimal (no
# sign, no leading or trailing point), and it never hands an exponent such as 1E999990 to
# Decimal, whose conversion to an integer would take seconds of CPU.
_AMOUNT = re.compile(r"([0-9]{1,18})(?:\.([0-9]{1,2}))?", re.ASCII)


def amount_to_cents(text: str) -> int:
    """Parse an amount such as 10.50 into cents. The value is not echoed in errors."""
    match = _AMOUNT.fullmatch(text)
    if match is None:
        raise ValueError(
            "must be ASCII digits with an optional point and at most two fraction digits"
        )
    whole, fraction = match.groups()
    return int(whole) * 100 + int((fraction or "0").ljust(2, "0"))


def format_datetime(moment: datetime) -> str:
    """UTC timestamp with milliseconds, e.g. 2026-11-25T10:15:30.123Z."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_datetime(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp has no time zone: {text!r}")
    return moment.astimezone(UTC)


def format_date(day: date) -> str:
    return day.isoformat()
