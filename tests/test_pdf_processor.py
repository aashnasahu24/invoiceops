from pathlib import Path
import shutil

import pytest

from invoiceops.pdf_processor import (
    InvalidPDFError,
    OCRUnavailableError,
    PDFLimitExceededError,
    PDFProcessor,
    PDFProcessorConfig,
    clean_extracted_text,
)
from invoiceops.schemas import DocumentType

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"


def test_digital_pdf_extracts_text_and_hash():
    path = SAMPLES / "happy_path_invoice.pdf"
    result = PDFProcessor().process(path)

    assert result.document_type == DocumentType.DIGITAL_PDF
    assert result.page_count == 1
    assert result.pages[0].method == "PDF_TEXT"
    assert "INV-2026-104" in result.full_text
    assert "PO-1001" in result.full_text
    assert len(result.file_sha256) == 64
    assert result.metadata.file_sha256 == result.file_sha256


def test_processor_accepts_uploaded_pdf_bytes():
    path = SAMPLES / "happy_path_invoice.pdf"
    result = PDFProcessor().process(path.read_bytes(), file_name="uploaded.pdf")
    assert result.file_name == "uploaded.pdf"
    assert "ABC Technologies" in result.full_text


def test_scanned_pdf_uses_ocr_when_tesseract_is_available():
    if shutil.which("tesseract") is None:
        pytest.skip("Tesseract executable is not installed")

    result = PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    assert result.document_type == DocumentType.SCANNED_PDF
    assert result.pages[0].method == "OCR"
    assert "INV-2026-221" in result.full_text
    assert "XYZ Solutions" in result.full_text


def test_file_extension_and_header_are_validated(tmp_path):
    not_pdf = tmp_path / "invoice.txt"
    not_pdf.write_text("not a pdf")
    with pytest.raises(InvalidPDFError):
        PDFProcessor().process(not_pdf)

    with pytest.raises(InvalidPDFError):
        PDFProcessor().process(b"not a pdf", file_name="invoice.pdf")


def test_size_limit_is_enforced():
    path = SAMPLES / "happy_path_invoice.pdf"
    config = PDFProcessorConfig(max_file_size_bytes=100)
    with pytest.raises(PDFLimitExceededError):
        PDFProcessor(config).process(path)


def test_page_limit_is_enforced(tmp_path):
    import pymupdf as fitz

    two_page_pdf = tmp_path / "two_pages.pdf"
    document = fitz.open()
    document.new_page()
    document.new_page()
    document.save(two_page_pdf)
    document.close()

    with pytest.raises(PDFLimitExceededError):
        PDFProcessor(PDFProcessorConfig(max_pages=1)).process(two_page_pdf)

    with pytest.raises(ValueError):
        PDFProcessorConfig(max_pages=0)


def test_text_cleaning_preserves_lines_and_normalises_whitespace():
    raw = "  Invoice\r\n\r\nVendor:\u00a0 ABC   Ltd\x0c\x00Total: 10  "
    cleaned = clean_extracted_text(raw)
    assert cleaned == "Invoice\n\nVendor: ABC Ltd\nTotal: 10"

@pytest.mark.parametrize(
    "filename",
    [
        "happy_path_invoice.pdf",
        "duplicate_invoice.pdf",
        "po_overrun_invoice.pdf",
        "split_invoice_1.pdf",
        "split_invoice_2.pdf",
        "blocked_vendor_invoice.pdf",
    ],
)
def test_all_digital_invoice_fixtures_are_readable(filename):
    result = PDFProcessor().process(SAMPLES / filename)
    assert result.full_text
    assert result.pages[0].method == "PDF_TEXT"
    assert result.document_type == DocumentType.DIGITAL_PDF


# --- table layout -----------------------------------------------------------------

def test_table_rows_stay_on_one_line():
    text = PDFProcessor().process(SAMPLES / "happy_path_invoice.pdf").full_text
    assert "Software License | 10 | 2,000.00 | 20,000.00" in text
    assert "Grand Total: | 29,500.00" in text
    assert "Invoice Number: INV-2026-104" in text  # ordinary words are not split into cells


def test_layout_preservation_can_be_disabled():
    text = PDFProcessor(PDFProcessorConfig(preserve_layout=False)).process(
        SAMPLES / "happy_path_invoice.pdf"
    ).full_text
    assert "Grand Total: | 29,500.00" not in text and "29,500.00" in text


# --- OCR trigger, quality and configuration ---------------------------------------

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract not installed")


def _scan_with_text_layer(footer: str, body_text: str = "") -> bytes:
    """A page that is one big scan image plus (optionally) real text on top."""
    import pymupdf

    src = pymupdf.open(SAMPLES / "scanned_missing_po.pdf")
    png = src[0].get_pixmap(dpi=150).tobytes("png")
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=png)
    page.insert_text((30, 830), footer, fontsize=7)
    if body_text:
        page.insert_textbox(pymupdf.Rect(30, 700, 560, 820), body_text, fontsize=8)
    return doc.tobytes()


@needs_tesseract
def test_scan_with_a_small_text_layer_footer_is_still_ocrd():
    """Regression: a scanner-app footer >30 chars used to suppress OCR entirely,
    so the whole invoice body was lost."""
    pdf = _scan_with_text_layer("Scanned with ScanApp v2.1 - page 1 of 1 - scan id 99812")
    result = PDFProcessor().process(pdf, file_name="scan.pdf")
    assert result.pages[0].method == "OCR"
    assert "INV-2026-221" in result.full_text
    assert result.document_type == DocumentType.SCANNED_PDF


def test_image_heavy_page_with_substantial_real_text_is_not_ocrd():
    body = "Invoice terms and conditions apply to every purchase made. " * 8  # >200 chars
    pdf = _scan_with_text_layer("footer", body_text=body)
    result = PDFProcessor().process(pdf, file_name="digital_with_background.pdf")
    assert result.pages[0].method == "PDF_TEXT"
    assert result.pages[0].ocr_confidence is None


@needs_tesseract
def test_ocr_reports_confidence_and_flags_poor_scans():
    result = PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    confidence = result.pages[0].ocr_confidence
    assert confidence is not None and 0 < confidence < 100
    assert result.metadata.ocr_min_confidence == confidence
    # The fixture is deliberately a poor scan: below the 75% policy threshold.
    assert any("Low OCR confidence" in w for w in result.warnings)

    lenient = PDFProcessor(PDFProcessorConfig(min_ocr_confidence=10)).process(SAMPLES / "scanned_missing_po.pdf")
    assert not any("Low OCR confidence" in w for w in lenient.warnings)


def test_digital_pdf_has_no_ocr_confidence():
    result = PDFProcessor().process(SAMPLES / "happy_path_invoice.pdf")
    assert result.pages[0].ocr_confidence is None
    assert result.metadata.ocr_min_confidence is None


def test_unavailable_tesseract_is_a_loud_error_not_silent_skip():
    config = PDFProcessorConfig(tesseract_cmd="/definitely/not/tesseract")
    with pytest.raises(OCRUnavailableError, match="TESSERACT_CMD"):
        PDFProcessor(config).process(SAMPLES / "scanned_missing_po.pdf")


@needs_tesseract
def test_tesseract_command_does_not_leak_between_processors():
    with pytest.raises(OCRUnavailableError):
        PDFProcessor(PDFProcessorConfig(tesseract_cmd="/definitely/not/tesseract")).process(
            SAMPLES / "scanned_missing_po.pdf"
        )
    # A later, correctly configured processor must still work in the same process.
    result = PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    assert result.pages[0].method == "OCR"
