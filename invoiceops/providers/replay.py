"""Disk-backed recording and replay providers for deterministic evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ..extraction_service import StructuredOutputProvider
from .errors import ProviderError

logger = logging.getLogger(__name__)


class ReplayCacheMissError(ProviderError):
    """No recorded raw response exists for a replay request."""


def cache_key(file_sha256: str, json_schema: dict[str, Any], model: str) -> str:
    if len(file_sha256) != 64:
        raise ValueError("file_sha256 must be a 64-character SHA-256 digest")
    schema_json = json.dumps(json_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    schema_hash = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{file_sha256}{schema_hash}{model}".encode("utf-8")).hexdigest()


def _instructions_hash(instructions: str) -> str:
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


class RecordingProvider:
    def __init__(
        self,
        inner: StructuredOutputProvider,
        cache_dir: str | Path,
        *,
        model: str | None = None,
        force_refresh: bool = False,
    ) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        resolved_model = model or getattr(inner, "model", None)
        if not resolved_model:
            raise ValueError("RecordingProvider requires a model name or an inner provider with a model")
        self.model = resolved_model
        self.force_refresh = force_refresh
        self.last_call_was_cache_hit = False

    def generate_structured_output(
        self,
        *,
        instructions: str,
        document_text: str,
        json_schema: dict[str, Any],
        file_sha256: str,
    ):
        self.last_call_was_cache_hit = False
        key = cache_key(file_sha256, json_schema, self.model)
        path = self.cache_dir / f"{key}.json"
        request_instructions = _instructions_hash(instructions)
        text_hash = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
        if path.exists() and not self.force_refresh:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(record, dict)
                and "response" in record
                and record.get("instructions_sha256") == request_instructions
            ):
                self.last_call_was_cache_hit = True
                return record["response"]

        self.last_call_was_cache_hit = False
        response = self.inner.generate_structured_output(
            instructions=instructions,
            document_text=document_text,
            json_schema=json_schema,
            file_sha256=file_sha256,
        )
        temporary_path = path.with_suffix(".json.tmp")
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(
                    {
                        "instructions_sha256": request_instructions,
                        "text_sha256": text_hash,
                        "response": response,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError:
            logger.warning("Could not write provider response to replay cache %s", path, exc_info=True)
        return response


class ReplayProvider:
    def __init__(self, cache_dir: str | Path, *, model: str) -> None:
        self.cache_dir = Path(cache_dir)
        self.model = model
        self.last_call_was_cache_hit = False

    def generate_structured_output(
        self,
        *,
        instructions: str,
        document_text: str,
        json_schema: dict[str, Any],
        file_sha256: str,
    ):
        self.last_call_was_cache_hit = False
        key = cache_key(file_sha256, json_schema, self.model)
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            raise ReplayCacheMissError(f"No replay response cached for request {key}")

        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(record, dict)
            and record.get("instructions_sha256")
            and record["instructions_sha256"] != _instructions_hash(instructions)
        ):
            raise ReplayCacheMissError(
                f"Replay instructions do not match the recorded fixture for request {key}"
            )
        self.last_call_was_cache_hit = True
        if isinstance(record, dict) and "response" in record:
            return record["response"]
        return record
