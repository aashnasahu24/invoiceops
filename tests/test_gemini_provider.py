from types import SimpleNamespace

import pytest

from google import genai
from google.genai import types

from invoiceops.providers.errors import (
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from invoiceops.providers.gemini_provider import GeminiProvider
from invoiceops.providers.gemini_provider import adapt_gemini_schema
from invoiceops.schemas import InvoiceExtractionLLM


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    def generate_content(self, **kwargs):
        self.request = kwargs
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, models):
        self.models = models


def response(text, **kwargs):
    return SimpleNamespace(text=text, **kwargs)


def provider_for(fake_response=None, error=None):
    models = FakeModels(fake_response, error)
    return GeminiProvider(
        api_key="test-key",
        model="test-model",
        client=FakeClient(models),
    ), models


def test_success_uses_json_mode_system_instruction_schema_and_data_contents():
    provider, models = provider_for(response('{"invoice_number":"INV-1"}'))
    schema = {"type": "object", "properties": {"invoice_number": {"type": "string"}}}

    result = provider.generate_structured_output(
        instructions="Extract only.",
        document_text="Invoice Number: INV-1",
        json_schema=schema,
    )

    assert result == {"invoice_number": "INV-1"}
    assert models.request["contents"] == "Invoice Number: INV-1"
    assert models.request["model"] == "test-model"
    assert models.request["config"] == {
        "system_instruction": "Extract only.",
        "temperature": 0,
        "response_mime_type": "application/json",
        "response_schema": schema,
    }


def test_gemini_schema_adapter_removes_unsupported_additional_properties_recursively():
    schema = InvoiceExtractionLLM.model_json_schema()
    adapted = adapt_gemini_schema(schema)

    def find_additional_properties(value):
        if isinstance(value, dict):
            if "additionalProperties" in value or "additional_properties" in value:
                return True
            return any(find_additional_properties(item) for item in value.values())
        if isinstance(value, list):
            return any(find_additional_properties(item) for item in value)
        return False

    assert schema["additionalProperties"] is False
    assert find_additional_properties(schema)
    assert find_additional_properties(adapted) is False
    assert "$defs" in adapted
    assert adapted["properties"] == schema["properties"]


def test_provider_sends_adapted_schema_not_mutated_source_schema():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "line_items": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": False},
            }
        },
    }
    original = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "line_items": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": False},
            }
        },
    }
    provider, models = provider_for(response("{}"))

    provider.generate_structured_output(
        instructions="Extract",
        document_text="Invoice",
        json_schema=schema,
    )

    assert models.request["config"]["response_schema"] == {
        "type": "object",
        "properties": {"line_items": {"type": "array", "items": {"type": "object"}}},
    }
    assert schema == original


@pytest.mark.parametrize(
    "text",
    ['```json\n{"invoice_number":"INV-1"}\n```', '```\n{"invoice_number":"INV-1"}\n```'],
)
def test_fenced_json_is_unwrapped(text):
    provider, _ = provider_for(response(text))
    assert provider.generate_structured_output(
        instructions="Extract",
        document_text="Invoice",
        json_schema={},
    ) == {"invoice_number": "INV-1"}


def test_malformed_and_non_object_json_are_response_errors():
    for text in ("{not-json}", "[]"):
        provider, _ = provider_for(response(text))
        with pytest.raises(ProviderResponseError):
            provider.generate_structured_output(
                instructions="Extract",
                document_text="Invoice",
                json_schema={},
            )


def test_rate_limit_error_is_mapped():
    error = RuntimeError("quota exceeded")
    error.code = 429
    provider, _ = provider_for(error=error)
    with pytest.raises(ProviderRateLimitError):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )


def test_quota_error_is_mapped_even_when_google_uses_403():
    error = RuntimeError("quota exceeded")
    error.code = 403
    provider, _ = provider_for(error=error)
    with pytest.raises(ProviderRateLimitError):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )


def test_timeout_error_is_mapped():
    provider, _ = provider_for(error=TimeoutError("request timed out"))
    with pytest.raises(ProviderTimeoutError):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )


def test_default_sdk_client_uses_30_second_timeout_and_disables_hidden_retries(monkeypatch):
    captured = {}

    def fake_sdk_client(**kwargs):
        captured.update(kwargs)
        return FakeClient(FakeModels(response("{}")))

    monkeypatch.setattr(genai, "Client", fake_sdk_client)
    GeminiProvider(api_key="test-key", model="test-model")

    options = captured["http_options"]
    assert isinstance(options, types.HttpOptions)
    assert options.timeout == 30_000
    assert options.retry_options.attempts == 1


def test_unavailable_error_is_mapped():
    error = RuntimeError("service unavailable")
    error.code = 503
    provider, _ = provider_for(error=error)
    with pytest.raises(ProviderUnavailableError):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )


def test_unmapped_sdk_error_is_mapped_to_unavailable():
    provider, _ = provider_for(error=RuntimeError("unexpected SDK failure"))
    with pytest.raises(ProviderUnavailableError):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )


def test_blocked_response_is_a_response_error():
    provider, _ = provider_for(
        response(
            "",
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
            candidates=[],
        )
    )
    with pytest.raises(ProviderResponseError, match="blocked"):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )


def test_empty_response_is_a_response_error():
    provider, _ = provider_for(response(None))
    with pytest.raises(ProviderResponseError, match="empty"):
        provider.generate_structured_output(
            instructions="Extract",
            document_text="Invoice",
            json_schema={},
        )
