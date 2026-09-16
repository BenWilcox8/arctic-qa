# Abstention evaluation

This document describes the abstention evaluation harness of ArcticQA.
The harness measures how often a model abstains when no listed option is correct.
It reuses the metrics of the previous abstention paper.
It does not build a separate abstention dataset.
Abstention is part of the evaluation of every item.

## Decisions

The captain locked these decisions on 2026-09-16.
The decision record is `data/arctic-abstention-eval/decisions.md` in the firstmate data folder.
The exact words of the captain are quoted here.

1. Distractor order.
   "Order the existing distractors at random but make sure that they're still ordered. And then drop the last one at test time."
   Every item carries one fixed random distractor order with its seed.
   The gold-present condition drops the LAST distractor of that order.
   There is no confidence measure and no rotation.
   "Let's do no rotation. It's fine, because it's just random."
2. Conditions.
   Each item runs as a pair: gold-present and gold-absent.
   "yes, as a pair, but in different sessions".
   Both conditions show the same number of options.
3. Option order.
   "Order should be shuffled randomly."
   All displayed options, the abstention option included, are shuffled with a recorded seed.
   "Make sure that the options are always shuffled between every model call, even the same model with the same effort level" (2026-09-16).
   The seed binds the model and the thinking arm, so every call has its own order.
4. Abstention wording.
   "wording should be "I abstain from answering"".
5. Output contract.
   "it should be single letter scoring with very clear prompting".
   "Scrap the reask. If the model doesn't output a single character, then just file that away for later revision".
   A response that is not exactly one option letter is invalid (N0).
   The raw response is stored, the trial is excluded from the metrics, and the N0 rate is reported per model.
6. Decoding.
   "Lets use the max temperature, and the reasoning will vary based on the official presets."
   "Lets start with medium reasoning to get a gauge on price".
   The temperature is the API maximum for the model and is recorded per run.
   The thinking level is an official preset and each preset is a separate arm.
   Repeats are a run parameter.
7. Scope.
   "build and test the infrastructure for a large run, but I will actually conduct the large scale run once the entire dataset is built".
8. Providers and models.
   "my budget is specifically in Google API credits, so I can only use Google AI models for the evaluation. But leave it open".
   "Let's only use pro models right now, but enumerate all of the different variants that I have access to".
9. Framing.
   "Let's focus on abstention specifically, so stick with what I previously had told you rather than saying no answer is scientifically defensible".

## Design

### Items and distractor order

A benchmark item is one accepted candidate with its gold answer and its accepted distractors.
The module `src/arctic_qa/distractor_order.py` defines the order contract `distractor-order-v1`.
The order is a seeded pseudo-random permutation of the distractor texts.
Generation records the order in the candidate field `distractor_order` with its seed.
The exporter and the live snapshot show the accepted distractors in that order.
An older candidate has no order field.
The evaluation set builder assigns the order with a seed keyed on the item id.
The manifest lists which items received an assigned order.

### Evaluation set

The module `src/arctic_qa/abstention_set.py` freezes one evaluation set.
It reads the state database read-only.
It selects items by population: `current`, `production`, or `list`.
The `current` population uses a contract file with the candidate schema, the generation prompt, and the scope contract.
The `production` population uses every accepted candidate whose run id starts with the production campaign prefix.
One item per paper family enters the set.
The builder keeps the first k accepted distractors of the order.
An item with fewer than k accepted distractors is excluded and listed with its reason.
The set directory holds `items.jsonl` and `manifest.json` with the item hashes.

### Conditions and rendering

The module `src/arctic_qa/abstention_render.py` renders one trial.
A trial is one item, one condition, one model, one thinking arm, and one repeat.
The gold-present condition shows the gold answer, the first k-1 distractors, and the abstention option.
The gold-absent condition shows all k distractors and the abstention option.
Both conditions show k+1 options.
The option order is a seeded permutation.
The seed depends on the set, the item, the condition, the repeat, the model and the thinking arm.
No two calls therefore share one order.
The captain ordered that on 2026-09-16: "Make sure that the options are always shuffled between every model call, even the same model with the same effort level".
Prompt version `abstention-eval-prompt-v1` shared one order across the models of one trial.
Version `abstention-eval-prompt-v2` is the current contract.
The seed stays deterministic and is recorded in the trial, the response row and the receipt.
The system instruction asks for exactly one uppercase letter.
The user content shows `QUESTION`, `QUESTION_CONTEXT`, and `OPTIONS` as separate blocks, as `docs/BENCHMARK_INPUT_CONTRACT.md` requires.
The prompt version is `abstention-eval-prompt-v2`.
The prompt hash binds the templates and the abstention text into the gate.

### Output contract

The Gemini provider asks for enum-constrained output with `text/x.enum`.
The enum holds the option letters of the trial.
The parser accepts only one uppercase letter from that set.
A different text, an empty response, or a finish reason other than `STOP` is invalid.
An invalid response is recorded as N0 with the raw text and the reason.
There is no re-ask.

### Isolation

One provider call answers one trial.
Each call is its own session with no history, no tools, and no provider storage.
The broker stores every request and response as an immutable receipt.
The request key binds the model, the item identity, the exact payload, the run id, and the trial id.
Two repeats of one identical stimulus never collide.

### Metering

Evaluation calls go through the shared paid-call ledger.
They run under the phase `benchmark_evaluation` and the stage family `evaluation_answer:<model>`.
The phase has its own budget policy, price config, and execution gate:

- `config/benchmark-evaluation-policy-v1.json` holds the evaluation ceiling and the operational controls.
- `config/benchmark-evaluation-prices-v1.json` holds one price entry, the thinking presets, and the pinned temperature of each evaluated model.
- `config/benchmark-evaluation-execution-gate-v1.json` is the disabled gate template.

The broker reports evaluation spend in `usage.benchmark_evaluation_usd` and in the `evaluation` block of the status record.
Evaluation spend counts toward the project lifetime ceiling.
It never counts toward the construction totals, the away ceiling, the checkpoint, or the per-paper cap.
Each evaluated item is its own ledger paper family.
A per-item repeat limit replaces the per-paper construction cap.
The construction price config and its transition chain stay unchanged.
The evaluation policy and price config are bound into the gate by hash.

### The evaluation ceiling

The evaluation ceiling is a money control, so a larger one needs a reviewed transition, chained on the applied predecessor, as a construction ceiling does.
The baseline is USD 5.00, the ceiling of the first two policies.
A policy with a ceiling at or below the authorized one needs no transition, because a smaller ceiling only tightens the control.
A policy with a larger ceiling is refused until its transition is applied.

The broker registers the exact authorized step in `EVALUATION_CEILING_CHANGES` of `src/arctic_qa/model_broker.py`.
One step is registered today: USD 5.00 to USD 200.00, the captain's allocation of 2026-09-16 for benchmarking the Gemini models.
`config/benchmark-evaluation-policy-v3.json` is the target policy, and it differs from v2 only in the ceiling, the policy id and the purpose.
Another ceiling needs its own constant and its own review before a transition file can apply.

The transition file has schema `benchmark-evaluation-policy-transition-v1` and these fields:

- `ledger_file`
- `from_evaluation_transition_sha256` (the applied predecessor event, or `null` for the first)
- `from_policy_file`
- `from_policy_sha256`
- `to_policy_sha256`
- `changed_policy_fields`
- `expected_ledger_sha256`
- `evaluation_gate_sha256`
- `integrated_code_commit`
- `review_record`
- `review_record_sha256`
- `reason`
- `authorized_at_utc`

The broker validates every one of them before it applies the transition.
The change set must be registered, the source policy must be present and match its hash, and the target must equal the source except the ceiling, the policy id and the purpose.
The gate must bind the target policy.
The ledger must validate, with no halt, no evaluation request in flight, no ambiguous evaluation charge, and an evaluation spend at or under the new ceiling.

The broker then writes one immutable `evaluation-policy-transition-<hash>.json` event in the receipt directory.
Every later start reads that event and needs no transition file.
Each new evaluation receipt records the event hash in `evaluation_policy_transition_sha256`.
A replaced event, an added event or a fork of the chain stops the broker.

The CLI option is `--evaluation-policy-transition-file`.

The gate binds the evaluation set manifest hash, the prompt version and hash, the abstention text, the model list, the arms, the decoding record, the repeat limit, and one run id.
The broker validates the gate and the binding before it reads the credential and before every request.
A changed manifest, prompt, model list, arm, decoding record, or run id stops before `countTokens`.

### Providers

The module `src/arctic_qa/abstention_providers.py` defines the provider interface.
The Google Gemini implementation sends every call through the shared broker.
The scripted transport answers by model and payload hash with no paid call.
The module `src/arctic_qa/abstention_subscription.py` adds two subscription providers.
See "Subscription providers" below.

### Subscription providers

Two providers bill a subscription instead of an API key: `anthropic_claude_code` and `openai_codex`.
The design record is `data/arctic-abstention-subscription-providers-r1/report.md`.
Each trial is one subprocess call of the installed harness binary from an empty scratch directory.
The system text and the user text are the bytes that the Gemini provider sends.

Claude runs through `claude -p` with `--model`, `--effort <arm>`, `--system-prompt <system text>`, `--tools ""`, `--setting-sources ""`, `--no-session-persistence`, `--disable-slash-commands`, `--strict-mcp-config`, `--permission-mode dontAsk`, `--permission-prompts none` and `--output-format json`.
The user text goes in on stdin.
Every `CLAUDE*`, `ANTHROPIC_*`, `OPENAI_*` and `CODEX_*` variable is removed from the child environment, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` removes the session-title side call.
The output is plain text.
`--json-schema` is not used because it adds a StructuredOutput tool.

ChatGPT runs through `codex exec` with `-m <model>`, `--ephemeral`, `--strict-config`, `--skip-git-repo-check`, `--ignore-rules`, `--json`, `--output-schema <file>`, `-c model_reasoning_effort=<arm>` and `-c model_instructions_file=<system text file>`.
The user text goes in on stdin.
The provider writes a private `CODEX_HOME` under the ledger directory with one strict `config.toml` and a symlink to the real `auth.json`.
That config disables web search, the shell tools, the subagent tools and the project documents, and shrinks the skills catalog to a stub.
`HOME` points to an empty directory so that no host skill is listed.
The schema is one object with one enum field `letter`.
The provider reads that field as the raw answer and stores the JSON message in the harness record.

The registry `config/benchmark-evaluation-subscription-models-v1.json` lists each vendor, its binary, the checked version, the models and their presets.
The gate binds this file by hash in the price-config slot and carries the provider name.
The decoding record binds the vendor, the harness version, the presets, the output constraint and the isolation flags.

Subscription calls do not enter the shared paid-call ledger.
They enter one subscription ledger per vendor (`--subscription-ledger-dir`).
That ledger applies the evaluation policy file: the per-item cap, the per-minute pace, one request at a time, no retry and stop on the first error.
It writes one immutable receipt per request key with `cost_usd: "0"`, the token counts, the invocation, the stdout and the stderr tail.
The response row carries a `harness` record with the vendor, the exact model id, the preset, the argv, the raw final text and the harness error lines.

Fairness caveats against the Gemini path:

1. Neither harness exposes temperature. Gemini runs at the API maximum. Claude and ChatGPT run at the provider default.
2. Each harness adds a residual prompt. Claude adds one SDK sentence, a billing header, a user-email reminder and an environment block (about 500 tokens). Codex adds tool descriptions, a permissions block, a skills stub and an environment block (about 1900 tokens). The evaluation prompt bytes are identical for all three.
3. The presets do not map one to one. Gemini has `low`, `medium`, `high`. Claude has `low`, `medium`, `high`, `xhigh`, `max`. Codex has `low` to `ultra` by model.
4. Gemini and Codex constrain the letter at the decoder. Claude has no decoder constraint, so its N0 rate includes prompt-compliance errors.

Setup:

- Claude: `claude auth status` must report `loggedIn: true` and `authMethod: claude.ai`. The binary is `/home/ben/.npm-global/bin/claude`.
- ChatGPT: `codex login status` must report "Logged in using ChatGPT". The binary is `/home/ben/.npm-global/bin/codex` and the login is `/home/ben/.codex/auth.json`.
- No `ANTHROPIC_API_KEY` and no `OPENAI_API_KEY` is read. The provider removes them from the child environment.
- The nix devshell has neither binary on PATH. The registry holds the paths. `--binary-path` overrides them.

### Scoring

The module `src/arctic_qa/abstention_score.py` scores one run.
It tallies N0 to N5 per model, condition, and arm.
It computes ACC, Precision_abs, Recall_abs, F1_abs, Abstention Rate, R-Acc, and SSR.
It also reports the narrow recall `N5 / (N4 + N5)` and the ideal pair rate.
The random baseline is a uniform guess over k+1 options in both conditions.
For k = 4 the baseline is ACC 0.200, Precision 0.500, Recall 0.125, F1 0.200, Abstention 0.200, R-Acc 0.125, SSR 0.300.
The test suite also reproduces the baseline of the previous paper (five and four options).
Confidence intervals come from a paired item bootstrap with a fixed seed.
The scorer writes `scores.json`, `main-table.csv`, `main-table.tex`, `condition-table.csv`, `letters.csv`, `contamination.csv`, `contamination.tex`, and `strata.csv`.
The contamination table shows the construction role of each evaluated model over the evaluated items.

## Procedure

Run every command from the repository root inside `nix develop`.
Prefix each command with `PYTHONPATH=src python -m arctic_qa --json abstention-eval`.

1. Build the evaluation set.

   ```bash
   --action build-set --state-db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
     --output-dir <sets-dir> --population current \
     --contract-file config/abstention-eval-chapter2-contract-v1.json
   ```

   Read `manifest.json`. Make sure that the item count and the exclusions are correct.
2. Render the trials and read one prompt.

   ```bash
   --action render --eval-set-dir <set-dir> --models gemini-3.1-pro-preview --arms medium --repeats 1
   ```

3. Run the dry run and its scores.

   ```bash
   --action dry-run --eval-set-dir <set-dir> --run-dir <dry-dir> --run-id <id> \
     --models gemini-3.1-pro-preview --arms medium --repeats 1 --scripted-policy random
   ```

   The dry run builds a private ledger in `<dry-dir>/ledger` and makes no paid call.
4. Write the gate template.

   ```bash
   --action gate-template --eval-set-dir <set-dir> --run-id <id> --models <models> --arms <arms> \
     --repeats <n> --review-record <review-file> --output-file <private-gate>
   ```

   An independent reviewer sets `independent_review_verdict` to `pass` and `evaluation_enabled` to `true`.
   The gate binds the code commit of the reviewed revision.
5. Run the paid evaluation.

   ```bash
   --action run --eval-set-dir <set-dir> --run-dir <run-dir> --run-id <id> \
     --models <models> --arms <arms> --repeats <n> \
     --evaluation-gate-file <private-gate> \
     --shared-ledger-file <ledger> --model-receipts-dir <receipts> \
     --streaming-budget-policy-file <active-construction-policy> \
     --price-config-file <active-construction-price-config> \
     --execution-gate-file <active-construction-gate> \
     --ledger-config-transition-file <active-transition> \
     --credential-file <key>
   ```

   The construction files must be the files that the production ledger already binds.
   The run appends one row per trial to `responses.jsonl`.
   A rerun resumes from that file and from the receipts.
   A budget stop or an ambiguous charge ends the run.
6. Score the run.

   ```bash
   --action score --run-dir <run-dir> --bootstrap 2000 --bootstrap-seed 7
   ```

### Canary

The action `canary` runs the current-contract items on one Pro variant.
It pins the `medium` preset and one repeat.
It refuses a model that is not a Pro variant.
It refuses an evaluation policy with a ceiling above USD 5.00.
It reports the cost per call, the tokens with thinking, the latency, and the invalid rate.
The canary runs only after the captain's price-gauge authorization.

### Model enumeration

The action `list-models` calls the free `models.list` endpoint.
It marks the Pro variants and joins each model with its local price entry and presets.
Pass `--models-file` with a saved response to run it offline.
With `--provider openai_codex` it reads the model catalog of the binary (`codex debug models`) and joins it with the registry.
With `--provider anthropic_claude_code` it lists the registry and the binary version.
The Claude binary validates no model id locally, so the API decides.

### Subscription run

Run every command from the repository root inside `nix develop`.
Prefix each command with `PYTHONPATH=src python -m arctic_qa --json abstention-eval`.

1. Make sure that the logins are present (see "Subscription providers").
2. Run the dry run of the vendor.

   ```bash
   --action dry-run --provider openai_codex --eval-set-dir <set-dir> --run-dir <dry-dir> \
     --run-id <id> --models gpt-5.6-terra --arms medium --repeats 1 --scripted-policy random
   ```

3. Write the gate template of the vendor.

   ```bash
   --action gate-template --provider anthropic_claude_code --eval-set-dir <set-dir> \
     --run-id <id> --models claude-opus-5 --arms medium --repeats <n> \
     --review-record <review-file> --output-file <private-gate>
   ```

   An independent reviewer sets `independent_review_verdict` to `pass` and `evaluation_enabled` to `true`.
   The gate binds the code commit, the harness version and the registry hash.
4. Run the evaluation.

   ```bash
   --action run --provider anthropic_claude_code --eval-set-dir <set-dir> --run-dir <run-dir> \
     --run-id <id> --models claude-opus-5 --arms medium --repeats <n> \
     --evaluation-policy-file config/benchmark-evaluation-policy-v1.json \
     --subscription-models-file config/benchmark-evaluation-subscription-models-v1.json \
     --evaluation-gate-file <private-gate> --subscription-ledger-dir <ledger-dir>
   ```

   The run appends one row per trial to `responses.jsonl` and one receipt per call to `<ledger-dir>/receipts`.
   A rerun resumes from both.
5. Score the run with `--action score`.

The launcher of the first live test is `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/subscription-test-r1-launcher.sh`.

## Concurrent plan

The captain fixed the evaluation plan on 2026-09-16.
Every question runs on eight models at the `high` reasoning preset.
Each model answers the question once with the gold answer present and once with it absent.
Each condition repeats three times.
One question is therefore 48 trials.

The plan file `config/benchmark-evaluation-plan-high-v1.json` holds that plan.
It lists the models of each vendor, the arms, the repeats, k, and the calls in flight per vendor.
The module `src/arctic_qa/abstention_plan.py` runs it.

| Vendor | Models | Calls in flight |
| --- | --- | --- |
| `google_gemini` | gemini-3.8-flash, gemini-3.7-flash | 4 |
| `anthropic_claude_code` | claude-fable-5-1, claude-opus-5, claude-sonnet-5 | 3 |
| `openai_codex` | gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra | 3 |

The three vendors always run at the same time.
Inside one vendor the runner keeps N calls in flight.
N is the smallest of the plan value, the policy limit of that vendor, and the `--concurrency` override.
The policy file `config/benchmark-evaluation-policy-v2.json` holds the per-vendor limits in its `vendors` block.
The Gemini limit is the headroom of the API key.
The two subscription limits stay low because those quotas are shared with every agent session on this machine.

One plan run directory holds one subdirectory per vendor.
Each vendor subdirectory is a complete serial-runner directory, so `--action score` reads it without a change.
The file `plan-manifest.json` binds the plan, and `plan-summary.json` holds the totals of the run.

Every invariant of the serial runner stays:

- One immutable receipt per request key.
- The caps of the evaluation policy.
- Resume from `responses.jsonl` and from the receipts.
- Stop on the first response that is not complete, per vendor.
  The calls already in flight finish and are recorded.
  The other vendors continue.
- One gate per vendor that binds the models, the arms, the repeats, the decoding record, the policy hash and the price-config hash.

### Concurrent evaluation requests in the shared ledger

The shared paid-call ledger serialised every request with one exclusive operation lock.
The evaluation phase now has its own concurrency slots and its own per-minute window.
Four changes make that safe:

1. A construction request still holds the exclusive operation lock for its whole call.
   An evaluation request never takes that lock.
   It serialises only its admission: the recovery, the gate check, the pace, `countTokens` and the reservation.
2. During the live call an evaluation request holds an in-flight lock file of its own request key.
   The lock files live in the hidden directory `.inflight` inside the receipts directory.
   Orphan recovery skips a submitted request whose lock another process holds.
   A crashed holder releases the lock, and the next recovery settles the request as before.
   Two processes must not share one evaluation run id.
   One reviewed gate authorizes one run id, so one launcher operates one run.
3. The concurrency check and the minute window count the requests of the active phase only.
   The construction view is the ledger total minus the evaluation requests in flight.
   An evaluation submission does not enter `recent_submission_times_utc`, so the construction pace stays exact.
4. An evaluation admission recovers only its own run.
   A construction run of another run id can be inside a live call with its response already durable and its settlement pending.
   A second recovery of that request settles it twice.

The tests `tests/test_abstention_plan.py` prove each of these four.

## Streaming evaluator

The module `src/arctic_qa/abstention_watch.py` evaluates every accepted question as soon as it appears.
It reads the production state database read-only.
It selects the accepted candidates of one campaign that match the population contract.
It freezes each one as a one-item evaluation set with its fixed distractor order and k = 4.
It runs the whole plan on that item.
Then it appends one row to the cost journal and waits for the next item.

### Authorization

A paid Gemini call needs a gate that binds one evaluation set.
A streaming run meets a new set at every item, so a human cannot review each gate in time.
The evaluator therefore needs one reviewed streaming authorization.
That file binds the population contract, the plan file, the evaluation policy, the price config, the subscription registry, the code commit, and two bounds: the item count and the Gemini USD.
From the authorization the evaluator derives one gate per item and per vendor.
Every field of a derived gate comes from the authorization.
Only the evaluation set identity and the run id change per item.
The authorization is the review record of every derived gate, and each derived gate stays on disk beside its item.
A changed contract, plan, policy, price config, registry or commit stops the evaluator before the first call.

### Cost journal

The module `src/arctic_qa/abstention_cost.py` writes one row per question to `cost-journal.jsonl`.
One row holds:

- The question id, its paper family and the campaign.
- The generation cost: the paid calls that the construction ledger booked to that item's paper family, with the calls per stage, and the campaign spend divided by the accepted items so far.
- The evaluation cost: the Gemini USD of that item in the evaluation phase of the shared ledger.
- The token counts of every Claude Code and Codex call, with the list-price equivalent of those tokens.
  The equivalent is information only.
  The calls bill the subscriptions at USD 0.
  The rates live in `config/benchmark-evaluation-list-prices-v1.json`, which no gate binds.
- The per-model N0 to N5 outcomes, the invalid rate, the wall time and the cumulative totals.

The action `cost-summary` prints the run so far: the accepted items, the USD per item on generation, the USD per item on Gemini evaluation, the subscription tokens per item, the projected cost of N items, and the per-model abstention metrics.
The metrics come from the scorer, so the summary and the tables cannot drift apart.

### Bounds and idempotency

The journal is the record of finished work.
A restarted evaluator reads it and never runs a completed question again.
The evaluator stops taking new items at the authorized item count.
A Gemini call never passes the evaluation ceiling: the broker refuses the call.
Before each item the evaluator also compares the remaining ceiling with the reservation that the item needs.
If one more item passes a bound, the evaluator pauses the Gemini vendor, writes a pause row in the journal, and keeps the subscription vendors running.
A vendor that stops on an item is paused the same way, whatever the reason: a budget stop, a harness error, a timeout or an ambiguous charge.
The policy forbids a retry, so the next item repeats that stop.

One stop is item-scoped and never pauses a vendor: the per-item repeat limit of the evaluation policy.
That limit counts the calls of one item, condition, model and arm, so it says nothing about the next item.
The evaluator records the stop on that item and takes the next one with every vendor still active.
The running service met this on 2026-09-16: a re-evaluated question exhausted its Gemini repeat budget, and the Gemini arm was then off for every later question.
The pause row names the vendor and the reason.
A start clears the vendor pauses the last invocation left and logs one `vendor_pause_cleared` event for each, because a start is an operator action that says to try again.
A model pause is not cleared by a start: it lives in the pause files, which an operator and the cost guard own.
The evaluator stops when every vendor is paused.
It exits non-zero only on a real error.

### Paused models

A model can be paused without a stop of the run.
Captain order 2026-09-16: "pause the fable evaluation because I only have ~80% fable usage left today; I will run the fable benchmarking after the reset at 6:00pm today".

The pause file is `config/benchmark-evaluation-model-pause-v1.json`, with schema `benchmark-evaluation-model-pause-v1`.
It holds one `paused_models` block.
Each key is a model id, and its entry takes a `reason` and an optional `resume_at_utc`.
A model with no resume time stays paused until an operator removes its entry.
A model whose resume time has passed is not paused any more.

The evaluator re-reads every pause file before every item.
So an operator or a cost guard can pause or resume a model while the evaluator runs, and the evaluator needs no restart.

`--pause-file` is repeatable, because the standing orders and a guard's own decisions have different owners.
Give the committed file first and the guard's file second: a later file wins for the same model.
The cost guard of `docs/BENCHMARK_GUARD.md` owns its file, marks its entries with `"owner": "benchmark-cost-guard"`, and removes only its own.
An entry an operator wrote stays exactly as written.
Without the option the evaluator reads the committed file alone.

The repeatable `--pause-model MODEL[=RESUME_UTC]` option pauses a model for one invocation, and it wins over every file for the same model.
`--no-pause-file` ignores every file.

The pause protects a real quota, so the pause file applies to `run-plan` and `watch`.
A dry run makes no call and uses no quota, so `dry-run-plan` reads only an explicit `--pause-model`.

A paused model's trials are held.
They are not dispatched, not recorded and not counted as invalid.
The item's journal row names the paused models in `evaluation.models_paused`, counts the held trials in `evaluation.pending_paused_trials`, and sets `evaluation.complete` to `false`.
The other models of the plan run on that item in the same pass.
An item whose every missing trial belongs to a model that is still paused waits: a revisit could record nothing and call nothing, so the evaluator leaves it alone and spends its poll on the items that can move.
When the pause lifts, the next pass takes the item up and runs only the trials that are missing, because the run directory keeps every recorded trial.
That pass appends a later row for the same item.
Read a run's totals through `CostJournal.latest_item_rows`, which keeps the last row of each item, so a revisited item is never counted twice.

This is the interface for a cost guard:

- write its own pause file (schema above) and give the evaluator that path as a second `--pause-file`;
- read `--action pause-status` with the same files to prove which models are held now;
- read `cost-journal.jsonl` in the work directory for the per-item cost, and `--action cost-summary` for the totals, the per-item averages and the projection.

```bash
--action pause-status --pause-file <pause-file>
```

The answer has schema `abstention-eval-pause-status-v1`, with `paused_now` and the entries the evaluator read.

### Procedure

1. Write the authorization for a reviewer.

   ```bash
   --action watch-authorization --state-db <state-db> --campaign-id <campaign> \
     --run-id-prefix <prefix> --contract-file <contract> --plan-file <plan> \
     --maximum-items <n> --maximum-gemini-usd <usd> \
     --review-record <review-file> --output-file <authorization>
   ```

   An independent reviewer sets `independent_review_verdict` to `pass` and `authorization_enabled` to `true`.

2. Operate the evaluator.

   ```bash
   --action watch --authorization-file <authorization> --plan-file <plan> \
     --contract-file <contract> --state-db <state-db> --work-dir <work-dir> \
     --shared-ledger-file <ledger> --subscription-ledger-root <root> \
     --ledger-run-prefix <ledger run prefix> --campaign-id <campaign> \
     --poll-seconds 30
   ```

   Add `--vendors` to run a subset of the plan.
   Add `--backfill` to evaluate the chapter 2 items too.
   Backfill is off by default.
   Add `--once` for one pass, or `--deadline-seconds` for a bounded test.
   Add `--pause-file` (repeatable) or `--pause-model` to hold one model's trials.
   Give the cost guard's own pause file as a second `--pause-file`.

3. Read the cost summary.

   ```bash
   --action cost-summary --work-dir <work-dir> --project-items 500
   ```

The evaluator runs as a systemd user unit, like the live publication snapshot service:

```sh
systemd-run --user --unit=arctic-abstention-stream-r1 \
  --working-directory=<worktree> <launcher>
journalctl --user -u arctic-abstention-stream-r1 -f
systemctl --user stop arctic-abstention-stream-r1
```

The launcher of the first live run is `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/streaming-eval-r1-launcher.sh`.
The work directory holds the sets, the derived gates, the runs, the cost journal and `watch-state.json`.
The evaluator writes `watch-state.json` after every poll, so its poll count and its item list rise while the unit runs.

A per-item plan manifest binds the run id and the code commit, and it is immutable.
So a new commit needs a new run id prefix and a new work directory.
Give the launcher both when the service is restarted from a new snapshot.

## Limits

- A paid Gemini evaluation call can meet one provider quirk that halts the shared ledger.
  gemini-3.7-flash returned a usage record without `candidatesTokenCount` on 2026-09-16, with a total that equals the prompt count plus the thinking count.
  The broker books that as an ambiguous charge, as `docs/SHARED_MODEL_BROKER.md` requires, and the halt stops every phase of the ledger.
  No reviewed settlement path covers an omitted answer-token count.
  Section 7 of `data/arctic-abstention-streaming-eval-r1/report.md` holds the evidence and the two ways to settle it.
  Read that section before the large Gemini run.

- The evaluation policy raises the per-minute pace only in the dry run. A paid run keeps the pace of the policy file.
- The rate window of the ledger is shared between phases. A construction run and an evaluation run pace each other.
- Only Gemini 3 models with `thinkingLevel` presets have price entries. A `thinkingBudget` model such as `gemini-2.5-pro` needs a new entry type before it can run.
- A subscription provider run needs a live harness login. An expired login fails the first call, the run stops, and the receipt holds the stderr tail.
- The Claude and Codex quotas are shared with every agent session on this machine.
- Every current item was written by `gemini-3.8-flash` and judged by `gemini-3.1-pro-preview`. The contamination table makes this visible. It does not remove the effect.
