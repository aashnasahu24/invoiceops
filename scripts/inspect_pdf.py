"""Inspect PDF extraction output from the command line.

Usage:
    python -m scripts.inspect_pdf data/sample_invoices/happy_path_invoice.pdf
"""

from __future__ import annotations

import argparse

from invoiceops.pdf_processor import PDFProcessingError, PDFProcessor


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract text from a digital or scanned invoice PDF")
    parser.add_argument("pdf", help="Path to the PDF file")
    args = parser.parse_args()

    try:
        result = PDFProcessor().process(args.pdf)
    except (PDFProcessingError, FileNotFoundError) as exc:
        parser.exit(status=1, message=f"Error: {exc}\n")

    print(f"File: {result.file_name}")
    print(f"SHA-256: {result.file_sha256}")
    print(f"Type: {result.document_type.value}")
    print(f"Pages: {result.page_count}")
    for page in result.pages:
        print(f"Page {page.page_number}: {page.method} ({page.character_count} chars)")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")
    print("\n--- EXTRACTED TEXT ---\n")
    print(result.full_text or "[No readable text]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
