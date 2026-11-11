# Stage 2 notes — Gemini extraction

## Schema and provider decisions

- `InvoiceExtractionLLM.model_json_schema()` remains the source of truth. The Gemini
  adapter copies and minimally adapts that schema before sending it to the official
  SDK as `response_schema`, requests `application/json`, and leaves response
  validation to the Pydantic service.
- Gemini Developer API schema adaptation removes `additionalProperties` recursively
  from a copy of the source schema. The live error showed the SDK serializing this as
  `additional_properties`, which the API rejects as an unknown field. Pydantic still
  rejects unexpected response fields in `InvoiceExtractionService`; `$defs` and
  nullable `anyOf` remain in the schema and are processed by the official SDK.
- The configured `gemini-3.8-flash` model is listed by Google as the stable model
  code, supports structured outputs, and is listed with free-tier availability. It
  was not the cause of the original 400.
- After removing `additionalProperties`, the schema smoke did not receive another
  400; it instead ended in `504 DEADLINE_EXCEEDED`. A separate minimal text request
  to the same configured model returned `503 UNAVAILABLE` with Google's message
  "This model is currently experiencing high demand." These are transient model
  capacity/deadline errors, not an invalid model ID or API-key rejection. The smoke
  script allows 90 seconds; production calls keep the 30-second timeout and retry
  transient 503/504 responses in the provider-agnostic wrapper.
- `gemini-2.5-flash` is not a good fallback: Google's current model page marks it
  deprecated. `gemini-3.5-flash-lite` is stable and explicitly targeted at document
  parsing, but switching is optional and should be tested on the labelled fixtures.
- Provider calls use the official `google-genai` SDK (`google.genai.Client` and
  `client.models.generate_content`) with the configured model, 30-second timeout,
  temperature 0, and the extraction instructions as `system_instruction`. SDK-level
  automatic retries are disabled so the provider-agnostic three-attempt wrapper is
  the only retry controller. The PDF text is passed separately as `contents` data.
- Provider rate-limit/quota errors, timeouts, service-unavailable errors, and invalid
  or blocked responses are mapped to explicit project exceptions. Only rate-limit,
  timeout, and unavailable errors retry. Validation errors get at most one retry
  whose system instruction includes the validation feedback.

## Observed failure modes

| Failure mode | Handling | Observed in a live Gemini call? |
|---|---|---|
| Schema contains `additionalProperties: false` | Remove that keyword before SDK serialization; Pydantic still forbids extras | Yes; exact 400 INVALID_ARGUMENT fixed |
| Gemini 3.8 Flash high demand / deadline | Transient 503/504; bounded retry in normal extraction; smoke script uses 90 seconds | Yes; minimal request returned 503 and schema call returned 504 |
| 429 / quota exhausted | `ProviderRateLimitError`; bounded exponential backoff and jitter | No; no credentials configured |
| Request timeout | `ProviderTimeoutError`; bounded retry | No; no credentials configured |
| Service unavailable / connection failure | `ProviderUnavailableError`; bounded retry | No; no credentials configured |
| Blocked, empty, malformed, or non-object response | `ProviderResponseError`; no retry | Offline fake-client tests only |
| Schema or extracted-value validation failure | One correction attempt with error feedback | Offline fake-provider tests only |
| Missing replay entry | `ReplayCacheMissError`, reported by evaluation | Offline replay tests only |

## Evaluation results

The Stage 2 live evaluation, reported by the project owner before Stage 3, scored
83/84 labelled fields across the original seven fixtures. The sole mismatch was the
scanned invoice's `po_number`: the document says `[NOT PROVIDED]`, but the model
returned that text rather than null. Stage 3 closeout now normalizes placeholders
before grounding and adds a warning while preserving the original only in the raw
provider response. An offline fake-provider test processes the scanned fixture and
confirms `[NOT PROVIDED]` becomes null. Tesseract reports 44.8% confidence locally,
so policy routes it to review. A replay attempt for this regenerated scan reported a
cache miss; it cannot confirm a new Gemini score.

| Fixture | Type | Field accuracy | Fully correct | Line items | Notes |
|---|---|---:|---:|---|---|
| Original digital fixture responses in local replay cache | Digital | 72/72 (100%) | 6/6 | 6/6 | Offline replay; the six cached digital records were correct |
| `scanned_missing_po.pdf` | Scanned | Not re-evaluated by Gemini | Not live-tested | Not live-tested | Replay cache miss after regeneration; offline placeholder test passes; OCR 44.8% LOW |
| `missing_invoice_number.pdf` | Digital | Not run live | Not run live | Not run live | New extraction-only case; replay cache miss |
| `tax_embedded_total.pdf` | Digital | Not run live | Not run live | Not run live | New extraction-only case; tax is embedded in the total; replay cache miss |
| `bundled_lump_sum.pdf` | Digital | Not run live | Not run live | Not run live | New extraction-only case; single bundled line; replay cache miss |
| `prompt_injection_line.pdf` | Digital | Not run live | Not run live | Not run live | New extraction-only case; prompt-like text is document data; replay cache miss |

| Run summary | Fields correct | Fully correct fixtures | Total time | Notes |
|---|---:|---:|---:|---|
| Owner-reported Stage 2 Gemini | 83/84 | Not individually reported | Not reported | Original seven fixtures; only miss was scanned `po_number` |
| Available local replay over all 11 fixtures | 72/132 (54.55%) | 6/11 | 6 cache hits | Five cache misses (four new fixtures plus regenerated scan) are counted as errors by the CLI and are not model mismatches |

The four new cases are deliberately not counted as passed: their labelled PDFs and
offline schema/grounding tests are available, but this workspace did not send them
to Gemini and they have no recorded responses for replay. The full replay CLI exit
was 1 because it encountered the five missing entries. Run the documented Gemini
evaluation to measure those cases and the scan. Do not loosen strict comparisons to
hide mismatches.
