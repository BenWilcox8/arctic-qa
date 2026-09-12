# Streaming dataset scheduler

The streaming scheduler processes one full-text paper at a time.
It runs scientific eligibility for each newly ready paper.
It immediately continues an eligible paper through QA, distractors, validation, and export.
It does not require a separate eligibility batch or manual stage relaunch.
It uses the answer-first method for the current commission.
It retains the direct-joint code without spending on that arm.

Automated acceptance does not establish scientific truth.
The strongest release label is `machine_accepted_unverified`.

## Offline command

Use fake scripts for an offline integration run:

```bash
PYTHONPATH=src python -m arctic_qa --json stream \
  --phase offline \
  --run-id offline-stream-r1 \
  --campaign-id streaming-commission-r1 \
  --access-run-dir /path/to/article-access-run \
  --eligibility-run-dir /path/to/eligibility-run \
  --eligibility-policy-file /path/to/frozen-eligibility-policy.json \
  --author-script fixtures/fake-author.jsonl \
  --verifier-script fixtures/fake-verifier.jsonl \
  --max-papers 1
```

The default progress file is:

`DATA_ROOT/arctic-qa/streaming-dataset-r1/progress.json`

It uses the `streaming-dataset-progress-v1` schema.
The file contains the full-text-ready, eligible, rejected, and accepted-QA counts.
It keeps at most 100 recent paper records.
For a brokered run, it also contains `broker_status_sha256` and `budget_policy_sha256`.
The broker refreshes those custody hashes after each durable ledger change.
After export, `dataset_metadata_sha256` binds the progress record to the export manifest.
The progress and export manifest use the campaign ID as their shared `run_id`.
The progress record keeps the command run ID separately as `invocation_run_id`.

Article-access item records can omit catalog fields that remain in the ordered selection.
The scheduler carries the selected authors and year into the source record.
It records a missing discipline as `unclassified` and does not guess a subject from the title or venue.

## Live command boundary

Live mode is disabled in the checked-in execution gate.
The exact integrated commit needs an independent review pass before the gate can change.
After an explicit pass of the complete integrated revision, current supervisor authorization permits a private gate and a tiny canary.
No new captain permission is required at that point.

After that review gate passes, use one of these phases:

- `live_test`
- `away_production`

Both phases must use the same central ledger and receipt directory:

```text
/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts/
```

Supply the credential through a private file with mode `0600`.
Its parent directory must not grant group or other access.
Do not put the key in a command, log, repository file, vault note, or export.

The live command shape is:

```bash
PYTHONPATH=src python -m arctic_qa --json stream \
  --phase live_test \
  --run-id live-test-r1 \
  --campaign-id streaming-commission-r1 \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/ARTICLE_ACCESS_RUN \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/ELIGIBILITY_RUN \
  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/ELIGIBILITY_POLICY.json \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --progress-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/progress.json \
  --max-papers 20
```

The broker checks the gate before it reads the credential.
It has no automatic retry, model fallback, or budget reset.
An unknown charge stops all later calls.

## Successful-path call budget

The current scheduler makes ten calls for one newly ready and accepted paper.
Eligibility is the first call in the same command.
Nine QA and distractor calls immediately follow an eligible decision.

| Stage | Calls | Input boundary | Output boundary |
| --- | ---: | ---: | ---: |
| Scientific eligibility, upstream | 1 | Counted before submission, at most 1,048,576 tokens | At most 8,192 tokens, including thinking |
| Finding and answer extraction | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Question generation | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Blinded reconstruction | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Answer verification | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Distractor generation | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Exact-option verification | 4 | Each call is counted before submission, at most 1,048,576 tokens | Each call is at most 2,048 tokens, including thinking |
| Downstream scheduler total | 9 | Nine separately counted inputs | 18,432 maximum requested output tokens across calls |
| Full new-paper total | 10 | Ten separately counted inputs | 26,624 maximum requested output tokens across calls |

The broker reserves each request from its exact counted input and configured output cap.
The verified price record uses USD 0.75 per million input tokens.
It uses USD 3.75 per million output and thinking tokens.
Each request must reserve no more than USD 0.25.
Each paper must use no more than USD 1.00.

The successful synthetic broker test reports 100 input, 10 candidate, and 5 thinking tokens for each call.
Its ten-call totals are 1,000 input, 100 candidate, and 50 thinking tokens.
Its fixture-priced ledger spend is USD 0.001320.
The nine downstream calls account for USD 0.001188 of that fixture total.
These values test accounting only.
They are not production measurements or yield estimates.

An earlier planning example used seven calls.
That estimate does not match the implemented path with four separate exact-option checks.
The implementation keeps all four checks and reports nine downstream calls.

## Resume and binding

The campaign ID freezes one finding for each paper family across live-test and production invocations.
Changing an invocation run name cannot authorize another paid request for the same model, stage, paper, family, and payload.

The local call journal binds each result to the provider, model, paper, family, immutable source SHA-256, schema, parameters, prompt, and candidate entity.
The central broker independently binds the model, stage, paper, family, immutable source SHA-256, and exact request payload.
The broker rejects a paper family that changes its source version.
It also rejects one source version assigned to two paper families.
Completed receipts are reused.
Ambiguous receipts stop the scheduler.

The scheduler records one accepted base question for each family only after at least three distractors pass all checks.
MCQ variants do not increase that count.
It preserves a valid short answer with fewer than three accepted distractors as `incomplete_non_mcq`.
That record uses a separate incomplete short-answer export.
It does not increase the 500-item target or headline `accepted_qa` count.
It exports an answer-present MCQ only with three accepted distractors.
It exports an absent-answer form only with four accepted distractors and the `invalid_option_set` label.

## Viewer command

The read-only viewer accepts the shared ledger, progress, budget policy, and export metadata files.
It never initiates a model call.

See `docs/CORPUS_VIEWER.md` for its full command.
