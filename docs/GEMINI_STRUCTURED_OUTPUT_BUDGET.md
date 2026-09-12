# Gemini structured-output budget correction

## Observed canary result

The first live-test eligibility request used `gemini-3.8-flash` with `thinkingLevel:"medium"`.
It used `maxOutputTokens:8192`, structured JSON, one candidate, temperature zero, and no tools.

The provider reported 7,629 thinking tokens and 549 candidate tokens.
The combined use was 8,178 of 8,192 tokens.
The 1,785-byte candidate ended inside a JSON string.
The provider reported `finishReason:"STOP"` instead of `MAX_TOKENS`.

This evidence supports output-budget pressure as a possible cause.
It does not prove that output-budget pressure caused the malformed response.
The finish reason conflicts with the documented cutoff behavior.

## Provider contract

Google documents `low` thinking for Gemini 3.8 Flash.
The same guide states that `max_output_tokens` includes thinking and candidate tokens.
It recommends a lower thinking level when a fixed limit can truncate the response.

Source: [Gemini thinking, thinking levels and token limits](https://ai.google.dev/gemini-api/docs/thinking#controlling-thinking), accessed 2026-09-12.

Google documents structured output for classification and extraction.
The supported JSON Schema subset includes object, array, required, `minItems`, and `maxItems`.
Google also requires application validation for semantic correctness.

Source: [Gemini structured outputs, JSON Schema support and best practices](https://ai.google.dev/gemini-api/docs/structured-output#json-schema-support), accessed 2026-09-12.

These sources describe provider behavior.
They do not establish scientific accuracy or successful output for this pipeline.

## Engineering adaptation

This change is an engineering correction, not a validated scientific contribution.
Configuration revision `arctic-gemini-eligibility-r1-config-v2` requires `thinkingLevel:"low"`.
The 8,192-token eligibility cap and all 2,048-token downstream caps stay unchanged.

The eligibility prompt asks for the shortest sufficient exact quote.
It prohibits article text outside evidence quotes.
It also asks for short reason codes and missing-context values.
The response schema and deterministic evidence checks stay unchanged.

The strict parser still rejects malformed JSON.
The broker still records completed billed responses and makes no automatic retry.

## Saved-response contract correction

The second live eligibility response returned complete JSON.
Its `request_id` and all four input hashes matched the request.
Its `schema_version` value was `1.0.0` instead of `eligibility-response-v1`.
Prompt version 1 did not state the required schema value as text.

Three proposed evidence strings were not exact source substrings.
The model removed layout whitespace or joined text from different columns.
Prompt version 1 already required exact quotes and supplied source-block locators.
These three errors are model noncompliance, not a missing evidence rule.

Prompt version 2 adds only the missing schema-value instruction.
It does not normalize quotes or relax deterministic evidence checks.
Prompt version 1 remains unchanged for the saved-response rejection path.
Prompt version 2 remains unchanged for its completed request.

Prompt version 3 is the default for new requests.
It discloses the existing unique-within-block validator rule.
It requires exact whitespace and Unicode preservation.
It tells the model to extend repeated text with adjacent exact text.
The validator remains byte-exact and unchanged.

This correction does not make the saved response valid.
It does not assign a scientific eligibility label.

## Offline counterfactual

The test `test_low_thinking_counterfactual_completes_the_structured_stream` uses the public streaming boundary.
It sends ten structured requests through the fake broker transport.
It requires low thinking for every request.
It also requires the unchanged output caps.

The fake transport returns complete schema-valid fixtures.
This test proves request construction and pipeline acceptance behavior only.
It does not predict live token allocation, provider conformance, or scientific correctness.

No network request or paid model call was used for this correction.

## Next live gate

Production remains unauthorized.
A focused independent review must pass for the revised exact commit.
Only then can the supervisor authorize one distinct bounded live-test item.
The next item must use the existing ledger with the USD 0.052620 charge.
The completed malformed request must never be retried or reset.
