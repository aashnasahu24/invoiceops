"""Google Gemini structured-output adapter."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_FENCED_JSON = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.IGNORECASE | re.DOTALL)


def adapt_gemini_schema(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Remove schema keywords unsupported by the Gemini Developer API.

    The Google Gen AI SDK converts Pydantic's ``additionalProperties: false``
    into an ``additional_properties`` wire field, which the Developer API
    rejects. Extra fields remain forbidden by InvoiceExtractionService's
    Pydantic validation, so removing this wire-level keyword does not weaken
    the application boundary.
    """
    def strip_additional_properties(value: Any) -> Any:
        if isinstance(value, list):
            return [strip_additional_properties(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: strip_additional_properties(item)
            for key, item in value.items()
            if key not in {"additionalProperties", "additional_properties"}
        }

    return strip_additional_properties(json_schema)


class GeminiProvider:
    """Call Gemini's JSON-output API while keeping the extraction boundary neutral."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 30,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("GEMINI_API_KEY must be configured")
        if not model.strip():
            raise ValueError("GEMINI_MODEL must be configured")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client if client is not None else self._create_client(api_key, timeout_seconds)

    @staticmethod
    def _create_client(api_key: str, timeout_seconds: float) -> Any:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - requirements install the SDK
            raise RuntimeError("Install the google-genai dependency to use GeminiProvider") from exc

        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def generate_structured_output(
        self,
        *,
        instructions: str,
        document_text: str,
        json_schema: dict[str, Any],
        file_sha256: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=document_text,
                config={
                    "system_instruction": instructions,
                    "temperature": 0,
                    "response_mime_type": "application/json",
                    "response_schema": adapt_gemini_schema(json_schema),
                },
            )
        except ProviderError:
            raise
        except Exception as exc:
            mapped = _map_provider_exception(exc)
            if mapped is None:
                mapped = ProviderUnavailableError("Gemini is temporarily unavailable")
            raise mapped from exc

        if _is_blocked(response):
            raise ProviderResponseError("Gemini blocked the extraction response")
        try:
            text = response.text
        except ValueError as exc:
            raise ProviderResponseError("Gemini returned no usable text response") from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError("Gemini returned an empty response")

        match = _FENCED_JSON.fullmatch(text.strip())
        if match:
            text = match.group(1)
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProviderResponseError("Gemini returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ProviderResponseError("Gemini response must be a JSON object")
        return result


def _is_blocked(response: Any) -> bool:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    if prompt_feedback is not None:
        reason = getattr(prompt_feedback, "block_reason", None)
        if reason and "UNSPECIFIED" not in str(reason).upper():
            return True

    for candidate in getattr(response, "candidates", None) or ():
        reason = getattr(candidate, "finish_reason", None)
        if reason and any(token in str(reason).upper() for token in ("SAFETY", "BLOCKED", "RECITATION")):
            return True
    return False


def _map_provider_exception(exc: Exception) -> ProviderError | None:
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    message = str(exc).upper()
    class_name = type(exc).__name__.upper()

    if status == 429 or "RESOURCE_EXHAUSTED" in message or "RATE_LIMIT" in message or "QUOTA" in message:
        return ProviderRateLimitError(f"Gemini rate limit or quota exceeded: {exc}")
    if status in (408, 504) or "TIMEOUT" in class_name or "TIMED OUT" in message:
        return ProviderTimeoutError(f"Gemini request timed out: {exc}")
    if status in (500, 502, 503) or any(
        token in class_name for token in ("CONNECTIONERROR", "CONNECTERROR", "NETWORKERROR")
    ):
        return ProviderUnavailableError(f"Gemini is temporarily unavailable: {exc}")
    if status is not None:
        return ProviderResponseError(f"Gemini API rejected the request (status {status}): {exc}")
    return None
