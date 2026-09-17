# Abstention evaluation

This document describes the abstention evaluation harness of ArcticQA.
The harness measures how often a model abstains when no listed option is correct.
It reuses the metrics of the previous abstention paper.
It does not build a separate abstention dataset.
Abstention is part of the evaluation of every item.

The commands of this document write path variables such as `$ARCTIC_QA_DATA_ROOT`.
The "Environment variables" section of `docs/REPRODUCTION.md` gives their values.

## Decisions

The captain locked these decisions on 2026-09-16.
The decision record is `arctic-abstention-eval/decisions.md` in the agent data folder.
It is outside this repository.
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
The design record is `research/arctic-abstention-subscription-providers-r1/report.md`.
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

- Claude: `claude auth status` must report `loggedIn: true` and `authMethod: claude.ai`. The registry holds the absolute path of the binary, `~/.npm-global/bin/claude` by default.
- ChatGPT: `codex login status` must report "Logged in using ChatGPT". The registry holds the absolute path of the binary, `~/.npm-global/bin/codex` by default, and the login is `~/.codex/auth.json`.
- No `ANTHROPIC_API_KEY` and no `OPENAI_API_KEY` is read. The provider removes them from the child environment.
- The nix devshell has neither binary on PATH. The registry holds the paths. `--binary-path` overrides them.
- The registry holds the absolute binary paths of the machine that produced the runs. The evaluation gate binds the registry by hash in the price-config slot, so an edit invalidates a gate of a live run. On another machine, pass `--binary-path`, or copy the registry to a new file with a new `config_id` and pass `--subscription-models-file`.

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
   --action build-set --state-db $ARCTIC_QA_DATA_ROOT/arctic-qa/state.sqlite3 \
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

The launcher of the first live test is `$ARCTIC_QA_DATA_ROOT/arctic-qa/abstention-eval/private/subscription-test-r1-launcher.sh`.

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

### Several questions at once

One question cannot fill the slots that the policy allows.
Its 12 Gemini trials run 4 at a time, and its 18 trials of each subscription vendor run 3 at a time.
So every vendor drains its wave and then waits for the slowest vendor of that question.
Measured at 08:35 UTC on 2026-09-17: 206 seconds of wall time per question, about 17 questions an hour, against a producer that accepted about 58 an hour.

The evaluator therefore takes up to `--item-workers` accepted questions at once.
The default is 8.
Every record of one question stays as it was: its own evaluation set, its own derived gates, its own run directory, and exactly one journal row per pass.
The slots stay as they were too, because a slot belongs to the vendor and not to the question:

- the calls in flight per vendor from the plan file;
- the per-minute window and the in-flight limit of the evaluation policy, which each subscription ledger applies under its own file lock;
- the per-phase in-flight slots and the minute window of the shared paid-call ledger.

The gain is that those slots stay full.
The cheap fast Gemini arm of one question never waits on the subscription arm of another.

Four rules hold the design:

- The pick-up order is the order the state database gives, and every reader is given that order back.
  A wave finishes its questions in whatever order the vendors answer, so `items_this_invocation` and `evaluated_items` are sorted back into the pick-up order.
  This is the rule the producer follows for its paper results.
- One admission is one critical section.
  The Gemini ceiling precheck, the active vendor list and the pause files are read under one lock, and the check keeps room for every question in flight plus the one it admits.
  Without that room, eight questions at once could pass the authorized Gemini USD bound by eight times one question's reservation.
- A question already in flight keeps the vendor list it started with.
  A vendor that stops is paused for every question dispatched after that, and each row records what the paused vendor owes its question.
- `watch-state.json` carries `items_in_flight` and `item_workers`, and the evaluator publishes it at every admission and every finish.
  One wave can run longer than the 900-second staleness bound of the cost guard.

One poll cycle scores four waves and then polls again.
The cycle does the work that belongs to no single question: it reads the shared ledger for a released ambiguous charge, it rebuilds the set of questions a paused arm holds, and it meets the questions the producer accepted since.
A wave of every pending question would hold all of that for as long as the backlog takes, which is hours at 68 pending questions.

A paused arm leaves every question of a wave open, and a later pass finishes them all in place.
A question is complete only when every active arm has its trials.

#### Every arm gets a share of the wave

The pick-up order is the oldest accepted question first, and a paused arm bends it.
Every question the arm held stays open and keeps its place at the front of the queue, so when the pause lifts a wave of them fills all eight slots, and the arms that already finished those questions idle.
That is what happened on 2026-09-17: the captain's Claude pause put 33 questions that owed Claude alone at the front, and the Gemini arm made no paid call between 10:03 and 11:06 UTC while 23 questions owed it trials.
The slow arm idled while the fast one worked, which is the opposite of what eight questions in flight are for.

`abstention_watch.wave_order` keeps a share of every wave for every arm that has a question to give it: `ceil(item_workers / vendors)` each, the arm with the fewest open questions served first, and the oldest question of that arm first.
The rest of the wave fills in the pick-up order.
The reorder runs before the wave is cut from the backlog, because a question an idle arm owes can be anywhere in it; beyond the waves the cycle keeps, the order is untouched.
No question is held back and the order inside every group is the pick-up order, so the fourth rule above still holds: every reader is given the pick-up order back.

`CostJournal.open_vendors_by_item` says what a question still owes, from the `outcomes_by_model` counts of its last row.
A question the journal has never seen owes every arm its whole plan.
Each poll emits a `wave_mix` event with the open questions per arm of the wave it chose.

### A stop that belongs to one question

A vendor that stops is paused for the rest of the invocation, because the policy forbids a retry.
A few stops are about one question only, and `abstention_watch.ITEM_SCOPED_REASONS` is that closed set:

- the per-item repeat limit of the evaluation policy, which counts the calls of one item, condition, model and arm;
- a busy exclusive operation lock of the shared paid-call ledger, which reserved nothing, submitted nothing and charged nothing.
  The producer treats the same refusal as a paper-level one and skips that paper.
  A wave meets it more often, because four Gemini threads queue on that lock, and it took the Gemini arm of the live evaluator down at 09:58 UTC on 2026-09-17 until an operator restarted the unit.
- the full concurrency slots and the full minute window of the evaluation phase, which are `model_broker.TRANSIENT_RESERVATION_REASONS`.
  The broker names those two together as the refusals that describe the moment and not the request: `execute` waits a bounded time for room before it records one, and the next question meets an emptier window.
  They took the arm down at 11:55 UTC on 2026-09-17, minutes after it became fast enough to fill the slots.

A reason belongs in that set only when the refusal reserved nothing, submitted nothing and charged nothing, and says nothing about the next question.

The busy exclusive lock has a second path.
A vendor reports it as a stop reason, which the set above covers; raised in the frame around the plan instead, it was this question's error, and one question's error halts the wave and ends the watch non-zero.
Every question of the wave raised it at 12:14:20 UTC on 2026-09-17 and the unit exited 1.
`run_item` now catches `BrokerOperationBusyError` there, emits an `item_lock_busy` event and leaves the question without a journal row, so a later pass runs its whole plan on it.

### A question an arm stopped inside

A pause reopens a question.
An arm that stopped *inside* a question does not reopen it.

The evaluation policy forbids a retry, so a trial that stops never goes out again under this contract.
Such a question keeps the trials it holds.
Its row records how many that is, in `evaluation.recorded_trials` against `evaluation.planned_trials`, and the row-level `complete` flag is `false`.
`CostJournal.completed_item_ids` closes it, because a revisit could add nothing by itself.
Reopening one is an operator action, because the recorded stop needs a judgement first.

A revisit of such a question never pauses the vendor again.
A recorded stop is permanent, so the vendor summary field `stopped_on` names it on every later pass.
The evaluator reads `stopped_this_pass` instead, which holds the stops of the rows that this pass recorded.
Otherwise the arm would go down again the moment a revisit touched an old stop, and a revisit is how a paused arm finishes the questions it owes.

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

A paused vendor owes its trials, exactly as a paused model does.
So an item evaluated while a vendor is paused is not complete, its journal row names that vendor in `evaluation.vendors_paused`, and a later invocation runs only the trials that are missing.
An item whose every missing trial belongs to a still-paused vendor waits instead of being revisited on every poll, because a revisit would record nothing.
`--vendors` is a different thing: it is the scope the operator chose, it lands in `evaluation.vendors_excluded`, and it owes nothing.
Before 2026-09-17 the two were one field and a paused vendor did not stop an item from being complete: the Claude Code arm stopped at 00:26:03Z when its binary was missing for a moment, and the next item was recorded complete with 30 of its 48 trials.

One pause lifts on its own: an ambiguous charge.
The broker keeps the reservation of a paid call whose charge it cannot prove, and it halts the evaluation phase of the shared ledger.
A supervisor releases that charge with a reviewed continuation, which is the `authorize-ambiguous-continuation` command of `docs/SHARED_MODEL_BROKER.md`.
The release is a ledger fact, so the evaluator asks the shared ledger before every question it admits.
At the first admission after the release, it resumes the Gemini vendor, logs a `vendor_resumed` event, and appends a `vendor_resume` row to the journal.
No restart is needed.
The question is asked at every admission and not only at the top of a poll cycle, because one cycle scores four waves: the arm stopped at 11:31:26 UTC on 2026-09-17 on a charge the ledger had already released, and it was dark for the rest of that cycle while the two subscription arms worked.
The ledger is read only while the arm is actually paused for an ambiguous charge, so an admission with nothing to resume pays nothing.
The evaluator resumes only the Gemini vendor this way, because the shared ledger is the record that proves the release.
A vendor paused for any other reason stays paused until a start clears it.
The 503 of 2026-09-16 at 17:53 UTC showed why: the Gemini arm, which the USD 200 allocation pays for, was off for every later question until an operator restarted the unit.

One stop is item-scoped and never pauses a vendor: the per-item repeat limit of the evaluation policy.
That limit counts the calls of one item, condition, model and arm, so it says nothing about the next item.
The evaluator records the stop on that item and takes the next one with every vendor still active.
The running service met this on 2026-09-16: a re-evaluated question exhausted its Gemini repeat budget, and the Gemini arm was then off for every later question.
The pause row names the vendor and the reason.
A start clears the vendor pauses the last invocation left and logs one `vendor_pause_cleared` event for each, because a start is an operator action that says to try again.
A model pause is not cleared by a start: it lives in the pause files, which an operator and the cost guard own.
The evaluator stops when every vendor is paused.
It exits non-zero only on a real error.

A bound is not an error, so the exit code alone never says that the benchmark stopped.
The unit met its item bound at 2026-09-16T19:31:44Z, exited 0, and no operator saw it until the next morning.
So `--status-file` takes the supervisor's status file, and the evaluator appends one `blocked:` line to it in three cases: the item bound ends the run, every vendor is paused, and a budget bound pauses the Gemini vendor.
That third case does not end the run, because the subscription vendors go on, but it turns off the arm the USD allocation pays for.
The cost guard of `docs/BENCHMARK_GUARD.md` watches the same stop from outside: it reports an evaluator whose watch state stopped moving.

### Stopping the unit

`SIGTERM` and `SIGINT` set the stop flag, which is read before every trial is
dispatched. The trials it holds are held exactly as a paused model's are: not
dispatched, not recorded, so the item is not complete and a later invocation
runs what is missing. The calls already on the wire finish and are recorded.

The bound of a stop is therefore one trial, not one item. Before 2026-09-17 the
flag was read only between two whole items, which is minutes of trials, and
systemd killed the unit at its 90-second stop bound at 07:25:55 UTC. The kill
left one free `counting` row in the shared ledger: nothing reserved, submitted
or charged, and the next start meets its own key and reuses it.

Give the unit a `TimeoutStopSec` longer than one trial of the slowest vendor.

### Paused models

A model can be paused without a stop of the run.
Captain order 2026-09-16: "pause the fable evaluation because I only have ~80% fable usage left today; I will run the fable benchmarking after the reset at 6:00pm today".

The pause file is `config/benchmark-evaluation-model-pause-v1.json`, with schema `benchmark-evaluation-model-pause-v1`.
It holds one `paused_models` block.
Each key is a model id, and its entry takes a `reason` and an optional `resume_at_utc`.
A model with no resume time stays paused until an operator removes its entry.
A model whose resume time has passed is not paused any more.

The evaluator re-reads every pause file before every item, under the admission lock of that item.
So an operator or a cost guard can pause or resume a model while the evaluator runs, and the evaluator needs no restart.
A question already in flight keeps the pause record it started with.

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
   Add `--status-file` with the supervisor's status file, so a bound that ends the run says so.

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

The launcher of the first live run is `$ARCTIC_QA_DATA_ROOT/arctic-qa/abstention-eval/private/streaming-eval-r1-launcher.sh`.
The work directory holds the sets, the derived gates, the runs, the cost journal and `watch-state.json`.
The evaluator writes `watch-state.json` after every item and after every poll, so its item list and its poll count rise while the unit runs.
One poll cycle covers every pending item, so at sixteen pending items a cycle runs for about an hour.
A watcher that published only at the end of its cycle would look stopped to the cost guard, whose staleness bound is 900 seconds.

A per-item plan manifest binds the identity of the item: the plan, the run id, the evaluation set, the arms, the repeats and the gate directory.
Those cannot move, so a new run id prefix still needs a new work directory.
Give the launcher both when the service is restarted from a new run id.

The vendor set and the code commit are not identity, and they grow.
A later pass adds a vendor the earlier pass could not reach, recomputes `trials_per_item` from that union, and appends its commit to `code_commits`; `code_commit` keeps the commit that opened the item.
So an item can be finished after a cutover, and the trials the earlier pass recorded are not run again.
Every response row names the commit that ran it, the first vendor run manifest stays exactly as it was written, and a later pass on another commit writes `run-manifest-<commit>.json` beside it.
Before 2026-09-17 the manifest was immutable field by field: two items whose manifest was written while the Claude Code arm was paused could never take that arm back.

## Limits

- A paid Gemini evaluation call can meet one provider quirk that halts the shared ledger.
  gemini-3.7-flash returned a usage record without `candidatesTokenCount` on 2026-09-16, with a total that equals the prompt count plus the thinking count.
  The broker books that as an ambiguous charge, as `docs/SHARED_MODEL_BROKER.md` requires, and the halt stops every phase of the ledger.
  No reviewed settlement path covers an omitted answer-token count.
  Section 7 of `research/arctic-abstention-streaming-eval-r1/report.md` holds the evidence and the two ways to settle it.
  Read that section before the large Gemini run.

- The evaluation policy raises the per-minute pace only in the dry run. A paid run keeps the pace of the policy file.
- The rate window of the ledger is shared between phases. A construction run and an evaluation run pace each other.
- Only Gemini 3 models with `thinkingLevel` presets have price entries. A `thinkingBudget` model such as `gemini-2.5-pro` needs a new entry type before it can run.
- A subscription provider run needs a live harness login. An expired login fails the first call, the run stops, and the receipt holds the stderr tail.
- The Claude and Codex quotas are shared with every agent session on this machine.
- Every current item was written by `gemini-3.8-flash` and judged by `gemini-3.1-pro-preview`. The contamination table makes this visible. It does not remove the effect.
