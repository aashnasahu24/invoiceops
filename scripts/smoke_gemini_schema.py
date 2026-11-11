"""Make one Gemini call to verify that the strict extraction schema is accepted."""

from __future__ import annotations

import sys
from pathlib import Path

from invoiceops.config import get_settings
from invoiceops.extraction_service import EXTRACTION_INSTRUCTIONS
from invoiceops.pdf_processor import PDFProcessor
from invoiceops.providers.errors import ProviderError
from invoiceops.providers.gemini_provider import GeminiProvider
from invoiceops.schemas import InvoiceExtractionLLM

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    settings = get_settings()
    if not settings.gemini_api_key or not settings.gemini_model:
        print("Set GEMINI_API_KEY and GEMINI_MODEL in .env before running the smoke call.", file=sys.stderr)
        return 2

    document = PDFProcessor().process(ROOT / "data" / "sample_invoices" / "happy_path_invoice.pdf")
    try:
        response = GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=90,
        ).generate_structured_output(
            instructions=EXTRACTION_INSTRUCTIONS,
            document_text=document.full_text,
            json_schema=InvoiceExtractionLLM.model_json_schema(),
            file_sha256=document.file_sha256,
        )
    except ProviderError as exc:
        print(f"Gemini schema smoke call failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Gemini schema smoke call succeeded for model {settings.gemini_model}; "
        f"received a JSON object with {len(response)} top-level fields."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
