import pytest

from invoiceops.normalization import (
    normalize_company_name,
    normalize_invoice_number,
    normalize_tax_id,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("INV-2026-104", "INV2026104"),
        ("inv 2026/104", "INV2026104"),
        ("  INV2026104 ", "INV2026104"),
        ("INV-0104", "INV0104"),  # leading zeros are meaningful
        ("", None),
        ("   ", None),
        ("---", None),
        (None, None),
    ],
)
def test_invoice_number(raw, expected):
    assert normalize_invoice_number(raw) == expected


def test_invoice_number_does_not_conflate_distinct_numbers():
    assert normalize_invoice_number("INV-0104") != normalize_invoice_number("INV-104")
    assert normalize_invoice_number("INV-0O4") != normalize_invoice_number("INV-004")  # O vs 0


def test_tax_id():
    assert normalize_tax_id(" 29abcde1234f1z5 ") == "29ABCDE1234F1Z5"
    assert normalize_tax_id("29-ABCDE-1234-F1Z5") == "29ABCDE1234F1Z5"
    assert normalize_tax_id(None) is None


@pytest.mark.parametrize(
    "a, b",
    [
        ("ABC Technologies Pvt Ltd", "ABC Technologies Private Limited"),
        ("ABC Technologies Pvt. Ltd.", "abc technologies"),
        ("Smith & Sons Co.", "Smith and Sons"),
        ("XYZ  Solutions", "xyz solutions"),
    ],
)
def test_company_names_that_should_match(a, b):
    assert normalize_company_name(a) == normalize_company_name(b)


def test_company_names_that_must_not_match():
    assert normalize_company_name("ABC Technologies") != normalize_company_name("ABC Tech")
    assert normalize_company_name("Delta Systems") != normalize_company_name("Delta Solutions")


def test_company_name_never_reduces_to_nothing():
    assert normalize_company_name("Limited") == "limited"
    assert normalize_company_name("") is None
    assert normalize_company_name(None) is None
