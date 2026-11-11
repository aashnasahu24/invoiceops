"""Provider-agnostic retry handling for extraction failures."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from ..extraction_service import (
    ExtractionError,
    ExtractionValidationError,
    InvoiceExtractionService,
    StructuredOutputProvider,
)
from ..pdf_processor import DocumentTextResult
from ..schemas import InvoiceExtraction
from .errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_TRANSIENT_ERRORS = (ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError)


class ExtractionFailed(Exception):
    """Extraction could not succeed after the configured attempts."""

    def __init__(self, reason: str, attempts: int, last_error: Exception) -> None:
        self.reason = reason
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"{reason} after {attempts} attempt(s): {last_error}")


class _FeedbackProvider:
    def __init__(self, inner: StructuredOutputProvider, feedback: list[str]) -> None:
        self.inner = inner
        self.feedback = feedback

    def generate_structured_output(
        self, *, instructions: str, document_text: str, json_schema: dict[str, Any], file_sha256: str
    ):
        prompt = (
            f"{instructions}\n\nYour previous response was invalid: "
            + "; ".join(self.feedback)
            + "\nReturn a corrected response that satisfies the schema."
        )
        return self.inner.generate_structured_output(
            instructions=prompt,
            document_text=document_text,
            json_schema=json_schema,
            file_sha256=file_sha256,
        )


def extract_with_retry(
    service: InvoiceExtractionService,
    document: DocumentTextResult,
    *,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
) -> InvoiceExtraction:
    """Retry transient provider errors and give schema failures one feedback retry."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")

    attempts = 0
    validation_retry_used = False
    feedback: list[str] | None = None
    last_error: Exception | None = None

    while attempts < max_attempts:
        attempts += 1
        service.last_attempts = attempts
        attempt_service = (
            service
            if feedback is None
            else InvoiceExtractionService(_FeedbackProvider(service.provider, feedback))
        )
        try:
            extraction = attempt_service.extract(document)
        except _TRANSIENT_ERRORS as exc:
            last_error = exc
            if attempts >= max_attempts:
                raise ExtractionFailed("Provider retries exhausted", attempts, exc) from exc
            jitter = 0.5 + random_value()
            sleep(base_delay_seconds * (2 ** (attempts - 1)) * jitter)
            continue
        except ProviderError as exc:
            service.last_raw_response = attempt_service.last_raw_response
            raise ExtractionFailed("Non-retryable provider error", attempts, exc) from exc
        except ExtractionValidationError as exc:
            service.last_raw_response = attempt_service.last_raw_response
            last_error = exc
            if validation_retry_used or attempts >= max_attempts:
                raise ExtractionFailed("Extraction validation retries exhausted", attempts, exc) from exc
            validation_retry_used = True
            feedback = exc.errors
            continue
        except ExtractionError as exc:
            service.last_raw_response = attempt_service.last_raw_response
            raise ExtractionFailed("Non-retryable extraction error", attempts, exc) from exc
        else:
            service.last_raw_response = attempt_service.last_raw_response
            return extraction

    assert last_error is not None
    raise ExtractionFailed("Extraction retries exhausted", attempts, last_error)
