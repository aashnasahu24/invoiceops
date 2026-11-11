import json

import pytest

from invoiceops.providers.replay import (
    RecordingProvider,
    ReplayCacheMissError,
    ReplayProvider,
    cache_key,
)


SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}
FILE_SHA = "a" * 64


class FakeProvider:
    model = "fake-model"

    def __init__(self):
        self.calls = 0

    def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
        self.calls += 1
        return {"value": "recorded"}


def test_recording_cache_is_reused_and_hit_is_visible(tmp_path):
    inner = FakeProvider()
    provider = RecordingProvider(inner, tmp_path)
    request = dict(instructions="extract", document_text="invoice", json_schema=SCHEMA, file_sha256=FILE_SHA)

    assert provider.generate_structured_output(**request) == {"value": "recorded"}
    assert provider.last_call_was_cache_hit is False
    assert provider.generate_structured_output(**request) == {"value": "recorded"}
    assert provider.last_call_was_cache_hit is True
    assert inner.calls == 1


def test_recording_cache_write_failure_still_returns_provider_response(tmp_path, monkeypatch, caplog):
    def fail_write(*_args, **_kwargs):
        raise OSError("cache is read-only")

    monkeypatch.setattr("pathlib.Path.write_text", fail_write)
    provider = RecordingProvider(FakeProvider(), tmp_path)
    response = provider.generate_structured_output(
        instructions="extract",
        document_text="invoice",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )
    assert response == {"value": "recorded"}
    assert "Could not write provider response to replay cache" in caplog.text


def test_recording_can_force_refresh_an_existing_entry(tmp_path):
    initial_inner = FakeProvider()
    initial = RecordingProvider(initial_inner, tmp_path)
    request = dict(
        instructions="extract",
        document_text="invoice",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )
    initial.generate_structured_output(**request)

    refresh_inner = FakeProvider()
    refreshing = RecordingProvider(refresh_inner, tmp_path, force_refresh=True)
    refreshing.generate_structured_output(**request)
    assert refreshing.last_call_was_cache_hit is False
    assert refresh_inner.calls == 1


def test_replay_reads_a_recorded_response_and_missing_key_raises(tmp_path):
    recording = RecordingProvider(FakeProvider(), tmp_path)
    request = dict(instructions="extract", document_text="invoice", json_schema=SCHEMA, file_sha256=FILE_SHA)
    expected = recording.generate_structured_output(**request)
    replay = ReplayProvider(tmp_path, model="fake-model")

    assert replay.generate_structured_output(**request) == expected
    assert replay.last_call_was_cache_hit is True
    with pytest.raises(ReplayCacheMissError):
        replay.generate_structured_output(
            instructions="extract",
            document_text="different invoice",
            json_schema=SCHEMA,
            file_sha256="b" * 64,
        )
    assert replay.last_call_was_cache_hit is False


def test_replay_rejects_fixture_recorded_with_different_instructions(tmp_path):
    RecordingProvider(FakeProvider(), tmp_path).generate_structured_output(
        instructions="original instructions",
        document_text="invoice",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )
    replay = ReplayProvider(tmp_path, model="fake-model")
    with pytest.raises(ReplayCacheMissError, match="instructions do not match"):
        replay.generate_structured_output(
            instructions="changed instructions",
            document_text="invoice",
            json_schema=SCHEMA,
            file_sha256=FILE_SHA,
        )
    assert replay.last_call_was_cache_hit is False


def test_replay_key_survives_ocr_text_variance_for_same_pdf(tmp_path):
    recording = RecordingProvider(FakeProvider(), tmp_path)
    recording.generate_structured_output(
        instructions="extract",
        document_text="OCR variant one",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )
    replay = ReplayProvider(tmp_path, model="fake-model")
    response = replay.generate_structured_output(
        instructions="extract",
        document_text="OCR variant two",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )
    assert response == {"value": "recorded"}
    assert replay.last_call_was_cache_hit is True


def test_cache_key_changes_for_file_schema_or_model_but_not_ocr_text():
    baseline = cache_key(FILE_SHA, SCHEMA, "model-a")
    assert cache_key("b" * 64, SCHEMA, "model-a") != baseline
    assert cache_key(FILE_SHA, {"type": "string"}, "model-a") != baseline
    assert cache_key(FILE_SHA, SCHEMA, "model-b") != baseline


def test_validation_feedback_bypasses_same_request_cache_key(tmp_path):
    inner = FakeProvider()
    provider = RecordingProvider(inner, tmp_path)
    provider.generate_structured_output(
        instructions="extract",
        document_text="invoice",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )

    response = provider.generate_structured_output(
        instructions="extract\nYour previous response was invalid: ...",
        document_text="invoice",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )

    assert response == {"value": "recorded"}
    assert provider.last_call_was_cache_hit is False
    assert inner.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_cache_file_contains_raw_response_json(tmp_path):
    provider = RecordingProvider(FakeProvider(), tmp_path)
    provider.generate_structured_output(
        instructions="extract",
        document_text="invoice",
        json_schema=SCHEMA,
        file_sha256=FILE_SHA,
    )
    saved = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert saved["response"] == {"value": "recorded"}
    assert saved["text_sha256"]


def test_recording_requires_model_identity_for_cache_keys(tmp_path):
    class ModelFreeProvider:
        def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
            return {"value": "recorded"}

    with pytest.raises(ValueError, match="requires a model name"):
        RecordingProvider(ModelFreeProvider(), tmp_path)
