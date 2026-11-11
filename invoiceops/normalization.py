"""Shared normalisation helpers for identity keys and fuzzy-free matching.

Normalisation is deliberately conservative. It removes formatting noise only
(case, punctuation, legal-form suffixes). It never "corrects" characters, so
`INV-0O4` and `INV-004` stay different; near-misses are a separate, softer check.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)

# Legal-form words removed from the *end* of a company name before comparison.
LEGAL_SUFFIXES = frozenset(
    {
        "private", "pvt", "limited", "ltd", "llp", "llc", "inc", "incorporated",
        "corp", "corporation", "co", "company", "plc", "pty", "gmbh",
    }
)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value).strip()
    return value or None


def normalize_invoice_number(value: str | None) -> str | None:
    """`INV-2026-104`, `inv 2026/104` and `INV2026104` share one identity key.

    Leading zeros are preserved (`INV-0104` != `INV-104`).
    """
    value = _clean(value)
    if value is None:
        return None
    return _NON_ALNUM.sub("", value.upper()) or None


def normalize_tax_id(value: str | None) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    return _NON_ALNUM.sub("", value.upper()) or None


def normalize_company_name(value: str | None) -> str | None:
    """`ABC Technologies Pvt. Ltd.` and `ABC Technologies Private Limited` match."""
    value = _clean(value)
    if value is None:
        return None
    text = value.casefold().replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    tokens = text.split()
    # Strip trailing legal-form words, but never reduce a name to nothing.
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or None
