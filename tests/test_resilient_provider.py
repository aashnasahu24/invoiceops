from invoiceops.extraction_service import ExtractionValidationError, InvoiceExtractionService
from invoiceops.pdf_processor import DocumentTextResult, PageText
from invoiceops.providers.errors import ProviderRateLimitError, ProviderResponseError
from invoiceops.providers.resilient import ExtractionFailed, extract_with_retry
from invoiceops.schemas import DocumentType, InvoiceExtractionLLM


def make_document():
    text = "Invoice Number: INV-1"
    return DocumentTextResult(
        file_name="test.pdf",
        file_sha256="a" * 64,
        document_type=DocumentType.DIGITAL_PDF,
        page_count=1,
        pages=(PageText(page_number=1, text=text, method="PDF_TEXT"),),
        full_text=text,
        warnings=(),
    )


class SequenceProvider:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.instructions = []

    def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
        self.instructions.append(instructions)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def valid_payload():
    return InvoiceExtractionLLM.empty_payload()


def test_transient_failures_back_off_exponentially_then_succeed():
    provider = SequenceProvider(ProviderRateLimitError("429"), ProviderRateLimitError("429"), valid_payload())
    service = InvoiceExtractionService(provider)
    delays = []

    result = extract_with_retry(
        service,
        make_document(),
        sleep=delays.append,
        random_value=lambda: 0.5,
    )

    assert result.invoice_number is None
    assert delays == [1.0, 2.0]
    assert len(provider.instructions) == 3


def test_non_retryable_provider_response_is_not_retried():
    provider = SequenceProvider(ProviderResponseError("blocked"))
    try:
        extract_with_retry(InvoiceExtractionService(provider), make_document(), sleep=lambda _: None)
    except ExtractionFailed as exc:
        assert exc.attempts == 1
        assert isinstance(exc.last_error, ProviderResponseError)
    else:
        raise AssertionError("Expected ExtractionFailed")
    assert len(provider.instructions) == 1


def test_non_retryable_extraction_error_is_wrapped_for_manual_review():
    class BadShapeProvider:
        def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
            return ["not", "an", "object"]

    try:
        extract_with_retry(InvoiceExtractionService(BadShapeProvider()), make_document())
    except ExtractionFailed as exc:
        assert exc.attempts == 1
        assert "JSON object" in str(exc.last_error)
    else:
        raise AssertionError("Expected ExtractionFailed")


def test_validation_error_gets_one_retry_with_feedback():
    provider = SequenceProvider({"unexpected": "field"}, valid_payload())
    service = InvoiceExtractionService(provider)

    result = extract_with_retry(service, make_document(), sleep=lambda _: None)

    assert result.invoice_number is None
    assert len(provider.instructions) == 2
    assert "Your previous response was invalid:" in provider.instructions[1]
    assert "unexpected" in provider.instructions[1]
    assert service.last_raw_response == valid_payload()


def test_transient_error_exhaustion_raises_extraction_failed():
    provider = SequenceProvider(
        ProviderRateLimitError("429"),
        ProviderRateLimitError("429"),
        ProviderRateLimitError("429"),
    )
    delays = []

    try:
        extract_with_retry(
            InvoiceExtractionService(provider),
            make_document(),
            sleep=delays.append,
            random_value=lambda: 0,
        )
    except ExtractionFailed as exc:
        assert exc.attempts == 3
        assert isinstance(exc.last_error, ProviderRateLimitError)
    else:
        raise AssertionError("Expected ExtractionFailed")
    assert delays == [0.5, 1.0]


def test_invalid_response_is_retried_only_once():
    provider = SequenceProvider({"bad": 1}, {"still": "bad"}, valid_payload())
    try:
        extract_with_retry(InvoiceExtractionService(provider), make_document(), sleep=lambda _: None)
    except ExtractionFailed as exc:
        assert exc.attempts == 2
        assert isinstance(exc.last_error, ExtractionValidationError)
    else:
        raise AssertionError("Expected ExtractionFailed")
    assert len(provider.instructions) == 2
