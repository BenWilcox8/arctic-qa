# Evaluation-phase 503: the reviewed release and the automatic vendor resume

Task `arctic-eval-503-release-r1`. Branch `fm/arctic-eval-503-release-r1`.

## 1. What happened

At 2026-09-16T17:53:46Z the streaming evaluator sent one paid Gemini call for the benchmark plan of item `aqa-0f42dabd7605064eacc5`.
The provider answered HTTP 503, status `UNAVAILABLE`, with the body "This model is currently experiencing high demand."
No usage came back, and no response was received.
The broker cannot prove whether the provider billed that call, so it booked an ambiguous charge.

| Field | Value |
| --- | --- |
| Request key | `ab9837334397dada182f05ea616d2e1e4fa00c060966679eeb11c5629b3768ce` |
| Phase | `benchmark_evaluation` |
| Stage | `evaluation_answer:gemini-3.7-flash` |
| Run | `abstention-stream-r10-aqa-0f42dabd7605064eacc5` |
| Family | `evaluation-item:aqa-0f42dabd7605064eacc5` |
| Trial | `abstention-trial-fd91d3435209f35bc659`, `gold_absent`, arm `high`, repeat 3 |
| Reserved | USD 0.030891 |
| Submitted | 2026-09-16T17:53:46Z (ledger row), 17:53:47Z (receipt) |
| Completed | 2026-09-16T17:54:38Z |
| Error class | `known_http_response_unknown_charge` |
| Timeout | 300 s, `retry_after` null, 228 input tokens |

The broker halted the evaluation phase only, through `evaluation_halted` with reason `ambiguous_generation_charge`.
The ledger halt stayed false, so the construction phase kept running under its own ceiling, as the phase-scoped halt intends.

The evaluator then paused the `google_gemini` vendor with reason `ambiguous_charge`, because its policy forbids a retry.
That pause held the Gemini arm the captain's USD 200 allocation pays for.

## 2. The evidence read

- Final receipt `ab9837334397....json`: `state` `ambiguous_charge`, `live_call_made` true, `http_status` 503, `actual_cost_usd` absent, no `response` block.
- Submitted receipt `ab9837334397....submitted.json`: `state` `submitted`, the same reservation and request digest.
- Request trace `ab9837334397....request-trace.json`: one `generateContent` call, `text/x.enum` answer schema, the `abstention-eval-prompt-v2` system text.
- No `ab9837334397....received.json` exists, so no usage was ever received.
- Ledger row: `state` `ambiguous_charge`, `phase` `benchmark_evaluation`, `gate_sha256` `e5ef0408...`, the same reservation.
- Six ambiguous rows stood in the ledger and their reservations summed to exactly `ambiguous_reserved_usd` 0.123539. Five already had reviewed continuation events; this one did not.

## 3. The records

Written in the chapter 2 shape under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-503-release-r1/`:

| File | sha256 |
| --- | --- |
| `ambiguous-continuation-evidence-ab983733-eval.json` | `3f05d9d3919dbc2874af3bcf7b46ac1fdc13d41cc5dd9917ab4644c5939f58f0` |
| `ambiguous-continuation-review-ab983733-eval.md` | `5adbeee382dc7787d02918feb349714227e8a38c9c9c5756f7b9fa1a1b9f1837` |

Reviewer: this task on firstmate's behalf, under the captain's standing order of 2026-09-16.
Decision: the reservation of USD 0.030891 stays in `ambiguous_reserved_usd` and counts against the evaluation phase; the review never replays, retries, or settles the request; the affected family stays in ambiguous custody; only unrelated trials continue.

## 4. Two defects the release met

The runtime could not release an evaluation-phase ambiguity at all. Two checks of `authorize_ambiguous_continuation` knew only the construction phase.

1. It required the global halt: `halted` true with reason `ambiguous_generation_charge`.
   A phase-scoped evaluation halt never sets those fields, so every release failed with "the ambiguous-charge halt state changed".
2. It validated the gate with `_validate_gate`, which accepts only `streaming-live-execution-gate-v1`.
   An evaluation request binds a `benchmark-evaluation-execution-gate-v1` gate, and the two gates name the authorized run in different fields: `authorized_run_id` against `authorized_new_run_id`.

The fix follows the pattern `reconcile_omitted_thought_usage` already uses.
The release now selects the gate and the run-id field by the phase of the request, and it lifts only the halt that phase set.
`authorize-ambiguous-continuation` takes `--evaluation-policy-file`, `--evaluation-price-config-file`, `--evaluation-gate-file`, and `--evaluation-policy-transition-file`.

## 5. The release

Run at 2026-09-16T18:27:18Z with the evaluator's own bound files: the chapter 3 construction policy v9, price config, execution gate and ledger transition, the evaluation policy v3 and prices of snapshot `a0b9a82`, and the derived per-item gate `streaming-r10/gates/aqa-0f42dabd7605064eacc5/google_gemini.json` (sha256 `e5ef0408aae51b00d5ebb1ca32bc31043abf05d3e78d417c7c331a8cde1395d1`).

Result: `applied` true, `replay_prohibited` true, `reserved_usd_retained` 0.030891.
Event `ambiguous-continuation-ab9837334397....json`, sha256 `9de89462342af7ef60e3d48bac100d416f6c6f80fc867d38c9c7d6fdc9dcc88b`, immutable.
`ledger_sha256_before` `77bb062fcabd319ebbe36b39b61c08f0ab4e01247fbb6313041f187c92265308`, `integrated_code_commit` `a0b9a82`, `skip_reason_code` `operational_ambiguous_charge_http_500`.

| Ledger | Before (18:13:33Z) | After (18:27:19Z) |
| --- | --- | --- |
| `halted` / `halt_reason` | false / null | false / null |
| `evaluation_halted` / reason | true / `ambiguous_generation_charge` | false / null |
| `spent_usd` | 69.533896 | 70.164378 |
| `ambiguous_reserved_usd` | 0.123539 | 0.123539 |
| `reserved_usd` | 0.021016 | 0.021016 |
| `inflight` | 0 | 0 |
| `count_requests` | 4823 | 4861 |
| `accepted_question_count` | 37 | 37 |

Broker status record after the release: `integrity_valid` true, `status_state` valid, `evaluation.phase_halted` false, evaluation ceiling USD 200.00, evaluation spend USD 3.860301, evaluation ambiguous USD 0.030891, evaluation remaining USD 196.108808 over 229 submissions.

The construction spend rose across the release, which is the phase-scoped halt working: the producer never stopped for this charge.

CAUTION: the release takes the exclusive broker operation lock without a wait, and the producer holds that lock for the whole of each paid call.
The retry loop that won the lock starved the producer, which exited at 18:26Z with "another paid broker operation is active".
Firstmate relaunched it at 18:28Z and dispatched `arctic-broker-operation-lock-wait-r1` for the real fix.
Run one such operation at a time, and tell the supervisor the minute you run it.

## 6. The vendor unpause

The evaluator has no resume for a vendor pause. `docs/ABSTENTION_EVALUATION.md` says a start clears the vendor pauses the last invocation left, so the documented way is a restart of the unit.

Stopped `arctic-abstention-stream-r3` at 18:27:54Z. It ended its poll cycle and exited cleanly at 18:28:24Z with `stopped_by_signal` true and no error.
Started it again at 18:28:36Z from the same reviewed snapshot `app-a0b9a82-arctic-abstention-streaming-eval-r1`, the same launcher, work directory `streaming-r10`, and authorization `streaming-eval-r6-authorization.json`.
No state was deleted.

The start logged, at 18:28:38Z:

```json
{"at":"2026-09-16T18:28:38Z","event":"vendor_pause_cleared","reason":"ambiguous_charge","vendor":"google_gemini"}
```

`watch-state.json` then read `"paused_vendors":{}` with `"active_vendors":["google_gemini","anthropic_claude_code","openai_codex"]`.

## 7. The automatic resume

A restart should not be the only way back. The release is a ledger fact, so the evaluator can read it.

`watch` now asks the shared ledger on every poll whether the evaluation phase is still halted.
When a vendor pause carries an ambiguous-charge reason and the phase is no longer halted, the evaluator resumes that vendor on that poll.
It removes the pause from the watch state, appends a `vendor_resume` row to the cost journal, and logs a `vendor_resumed` event.
Only `google_gemini` resumes this way, because the shared ledger is the record that proves the release.
A vendor paused for any other reason stays paused until a start clears it.

The halt rule moved to one public function, `model_broker.phase_halt_reason`, which `SharedGeminiBroker._phase_halted` now calls. The evaluator reads the same rule the broker enforces.

Tests in `tests/test_abstention_watch.py` drive the real loop over two polls, with the release and one more accepted question between them:

- `test_a_released_ambiguous_charge_resumes_gemini_on_the_next_poll`: the vendor pauses with reason `ambiguous_charge` on the first poll, resumes on the second, and runs all 48 trials of the next question. The journal holds one `vendor_resume` row.
- `test_an_unreleased_ambiguous_charge_keeps_gemini_paused`: a standing evaluation halt keeps the pause, and the next question records `vendors_paused` `["google_gemini"]`.
- `test_a_clean_ledger_does_not_resume_a_vendor_paused_for_another_reason`: a harness failure keeps the pause even with a clean ledger.
- `test_the_ambiguous_charge_reason_is_read_from_the_request_state`: the reason shapes the classifier accepts and refuses.

## 8. The re-snapshot

A new read-only runtime snapshot of the landed commit is under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-abstention-streaming-eval-r1/runtime/`.

The running unit still runs `a0b9a82`, and switching it is a supervisor decision, not this task's.
`data/arctic-abstention-streaming-eval-r1/report.md` section 8.2 gives the reason: a per-item plan manifest binds the run id and the code commit and is immutable, so a restart from a new commit needs a new snapshot, a new run id prefix, a new work directory, and a new reviewed authorization.

Today that switch would cost more than it gives:

- Ten items in `streaming-r10` are incomplete only because of the captain's `claude-fable-5-1` pause, which lifts at 2026-09-16T23:00:00Z. The evaluator must revisit each one in the same work directory to run the held fable trials.
- A revisit under another commit fails in `run_plan` with "the run directory holds a different plan manifest". `watch` then records the error and ends the loop, so the evaluator would exit non-zero on its first revisit.
- A new work directory avoids that failure by abandoning every recorded trial of the ten items. Their 48 trials each would run again, about USD 1.6 of Gemini spend and 360 subscription calls, and the journals would hold the same questions twice.

The natural boundary is the item bound. The authorization allows 12 items and 10 are recorded, so two more questions end this invocation with `item_bound_reached`.
A new authorization is needed then in any case, and the new snapshot fits that restart with no waste.

The documented restart already covers the same fault until then.

## 9. Two faults for a supervisor

1. A visit rewrites the derived per-item gate with a new `written_at_utc`, so its digest changes. A release of an ambiguous charge needs the exact digest its receipt recorded, so a revisit makes that charge unreleasable forever. This release ran before the fable pause lifts at 23:00 UTC, which is when the ten held items get their revisit. `write_evaluation_gate` should not rewrite a gate whose other fields are unchanged.
2. A cost row's `evaluation.complete` is `pending_paused_trials == 0`, which counts held *model* trials only. An item whose Gemini trials a *vendor* pause dropped is therefore complete, and the evaluator never returns to it. The benchmark then holds a silent hole for that question. The production items hid this because a model pause held each of them as well.

Neither is in this task's scope. Both are in the same family as the fault this task fixed.

## 10. Commands

```bash
# The release, from a checkout of this branch.
PYTHONPATH=src python -m arctic_qa --json authorize-ambiguous-continuation \
  --request-key ab9837334397dada182f05ea616d2e1e4fa00c060966679eeb11c5629b3768ce \
  --expected-ledger-sha256 LEDGER_SHA256 \
  --review-file $D/ambiguous-continuation-review-ab983733-eval.md \
  --evidence-file $D/ambiguous-continuation-evidence-ab983733-eval.json \
  --authorized-run-id abstention-stream-r10-aqa-0f42dabd7605064eacc5 \
  --operator-id fm-task:arctic-eval-503-release-r1 \
  --streaming-budget-policy-file $R/streaming-dataset-budget-policy-v9-chapter3.json \
  --price-config-file $R/runtime/app-c545cf8-arctic-ch3-production-run-r1/config/gemini-eligibility-v1.json \
  --execution-gate-file $R/live-execution-gate-c545cf8-ch3.json \
  --ledger-config-transition-file $R/ledger-config-transition-9f4cb18-ch3.json \
  --evaluation-policy-file $APP/config/benchmark-evaluation-policy-v3.json \
  --evaluation-price-config-file $APP/config/benchmark-evaluation-prices-v1.json \
  --evaluation-gate-file $E/streaming-r10/gates/aqa-0f42dabd7605064eacc5/google_gemini.json \
  --shared-ledger-file $L/shared-paid-call-ledger.json \
  --model-receipts-dir $L/model-receipts \
  --credential-file /home/ben/.config/arctic-qa/gemini-api-key \
  --prior-construction-spend-usd 0

# The unit.
systemctl --user stop arctic-abstention-stream-r3
systemd-run --user --unit=arctic-abstention-stream-r3 --working-directory=$APP \
  /mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/streaming-eval-r6-launcher.sh
journalctl --user -u arctic-abstention-stream-r3 -f
```

## 11. The suite

`nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'`.
