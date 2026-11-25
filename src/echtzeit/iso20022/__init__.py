"""ISO 20022 messages for SEPA Instant: pacs.008.001.08 and pacs.002.001.10.

These modules read and write a subset of the messages, following the EPC SCT Inst
Inter-PSP Implementation Guidelines 2025. They are not a certified implementation.
"""

from echtzeit.iso20022 import identifiers, pacs002, pacs008

__all__ = ["identifiers", "pacs002", "pacs008"]
