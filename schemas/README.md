# Schemas

This directory contains the JSON Schemas for records that cross a pipeline boundary.
A schema states which fields exist, which fields are required, and which value types are valid.

The code validates records against these contracts before it stores or exports them.
Older schema versions remain here because historical records still identify those versions.

## Files

| File | Record |
| --- | --- |
| `corpus-progress.v1.schema.json` | Progress from a corpus stage, including counts, status, and the current message. |
| `gemini-eligibility.v1.schema.json` | The first five-criterion eligibility response. |
| `gemini-eligibility.v2.schema.json` | Eligibility with the second response contract and its evidence fields. |
| `gemini-eligibility.v3.schema.json` | Eligibility with structured evidence spans. |
| `gemini-eligibility.v4.schema.json` | The current eligibility response, including all five criteria in a geography re-screen. |
| `item.v1.schema.json` | The first exported ArcticQA candidate record. |
| `item.v2.schema.json` | The current candidate record with combined source evidence. |
| `metadata-prefilter.v1.schema.json` | The disposition of one paper during metadata-only filtering. |
| `source-manifest.v1.schema.json` | One content-addressed source record in a frozen manifest. |
| `source-screening.v1.schema.json` | One reviewed source-screening overlay revision. |

## How schemas flow through a run

1. Discovery writes source records that conform to `source-manifest.v1.schema.json`.
2. The metadata prefilter writes dispositions that conform to `metadata-prefilter.v1.schema.json`.
3. The screening tools write overlay records that conform to `source-screening.v1.schema.json`.
4. The eligibility provider returns a response that conforms to the selected `gemini-eligibility` schema.
5. Generation and validation produce a candidate that conforms to the selected `item` schema.
6. The corpus viewer reads progress records that conform to `corpus-progress.v1.schema.json`.

The provider accepts only a restricted subset of JSON Schema keywords.
`tests/test_eligibility_request_constraints.py` protects that live-provider boundary.

Do not change an existing version to give it a new meaning.
Add a new version when a record contract changes.
