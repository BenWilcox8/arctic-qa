# Chapter 2 timeout recovery, branch fm/arctic-ch2-timeout-recovery-r1

Branch base: local `main` at commit `00a6762`.
Scope: one more bounded ambiguous-continuation case, its regression tests, and a per-stage call timeout.

## 1. The new bounded case: a provider timeout

Run `chapter2-e8d4cad-r1` halted for a third time at 2026-09-15T17:31:24Z.
Request `daf405de9755e0c7139779e211ec53a7d38c525e9f37d729256ac4fe9604a1bb` (stage `answer_verification`, model `gemini-3.1-pro-preview`, paper `10.1371/journal.pone.0096079`, family `family-aeed4bfe62a1e1028954`) ended with a client timeout.
No release path admitted it.
The 5xx case needs an HTTP status, the received-max-tokens case needs a response, the orphaned path needs a dead owner with no final receipt, and the pretransport settlement needs `live_call_made` false.

`_is_provider_timeout_ambiguous_case` in `src/arctic_qa/model_broker.py` admits the case.
All of these conditions are required, and the ledger request row supplies the reservation that the receipt must match:

- the final receipt state is `ambiguous_charge`;
- `error` is the exact string `TimeoutError: provider outcome unknown`, which is the string the transport failure path writes at `model_broker.py` in the generic `except Exception` branch;
- `live_call_made` is `true`;
- the receipt has no `response` key;
- the receipt has no `error_class` key and no `http_status` key;
- no received receipt file exists next to the final receipt;
- `reserved_usd` equals the reservation of the ledger request;
- `actual_cost_usd` is null.

The two exclusions (`error_class`, `http_status`) make the three cases disjoint by structure, not by order of tests.
A 5xx receipt carries `error_class` and `http_status` and carries no `error` key, so it can never match this case.
A received-max-tokens receipt carries a `response` and a different `error` string, so it can never match this case either.

### Evidence

The case has its own evidence schema, `shared-paid-call-provider-timeout-continuation-evidence-v1`.
It is the same schema family as the 5xx evidence.
It replaces the `http_status` field of that dict with `error` and `timeout_seconds`:

```json
{
  "schema": "shared-paid-call-provider-timeout-continuation-evidence-v1",
  "request_key": "<64 hex characters>",
  "error_class": "provider_timeout_unknown_charge",
  "error": "TimeoutError: provider outcome unknown",
  "timeout_seconds": 120,
  "live_call_made": true,
  "received_receipt_absent": true,
  "actual_cost_known": false,
  "replay_prohibited": true,
  "affected_family_id": "<family id>",
  "authorized_run_id": "<run id>"
}
```

`authorize-ambiguous-continuation` refuses the release when the evidence file is not exactly this dict.
The stored continuation event uses schema `shared-paid-call-provider-timeout-continuation-v1`, error class `provider_timeout_unknown_charge`, and skip reason code `operational_ambiguous_charge_provider_timeout`.
The event carries no `http_status` field.
`_validate_provider_timeout_continuation` applies the same field-set check, the same value checks, and the same exact-evidence check on every later broker start.
`_ambiguous_continuation_events` re-tests the receipt against the same predicate and against the event `timeout_seconds`.

### Which timeout the evidence names

`_receipt_timeout_seconds` reads `timeout_seconds` from the final receipt.
A receipt written before the timeout became a stage model fact carries no such field.
Such a call ran under the one fixed 120 second transport timeout, so the function returns 120 for it.
The current configuration is never substituted.
This matters for `daf405de...`: the `answer_verification` stage now carries 300 seconds, but that call was given 120 seconds and stopped at 121 seconds.
The evidence for that request must name 120.

### The rigor argument

The reservation stays reserved.
`authorize_ambiguous_continuation` writes the continuation event, then clears `halted` and `halt_reason`.
It changes no money field.
The request row keeps state `ambiguous_charge`, and its reservation stays in `ambiguous_reserved_usd`, which counts against the per-request cap, the away-session ceiling and the cumulative ceiling of USD 108.994972.
The reservation policy recorded in the event is `retain_full_reservation_in_ambiguous_reserved_and_count_against_all_caps`, the same policy the 5xx case records.

A timeout cannot release a charge that was in fact billed, because the code never settles it.
The client stopped waiting; it did not learn that the provider stopped.
The provider can have finished the generation and billed it.
The broker therefore keeps the full reservation as if the call were billed at its reserved price.
No later step can settle it from this path: settlement needs a received receipt with provider usage, and the usage reconciliation path is separate and needs its own review record.

The request is never replayed.
The family `family-aeed4bfe62a1e1028954` stays in operational custody through `operational_unresolved_families`, which now reports the new skip reason code.
A second call with the same request key is refused with "the paid request key already exists".
Only unrelated papers continue.
The release is also all-or-nothing: `authorize_ambiguous_continuation` still refuses while any other ambiguous request of the run has no continuation event.

## 2. The per-stage call timeout

The timeout was a fixed 120 seconds in `GeminiTransport`.
`gemini-3.1-pro-preview` thinks before it answers, so 120 seconds cut live judge calls off while the provider was still working.
Each cut left a charge that is unknown and a reservation that must stay reserved.

The timeout is now a stage model fact in the price config, next to the model and its rates.
`config/gemini-eligibility-v1.json` moves from `arctic-gemini-eligibility-r1-config-v6` to `arctic-gemini-eligibility-r1-config-v7`.
The v7 revision adds `"call_timeout_seconds": 120` at the top level and `"call_timeout_seconds": 300` to the four `gemini-3.1-pro-preview` stages: `standalone_verification`, `option_verification`, `blinded_reconstruction` and `answer_verification`.
No price, model, token limit or thinking level changes.

`_config` pins the judge timeout at exactly 300 seconds for v7 and refuses the field for v6, so the meaning of a config revision never changes in place.
`call_timeout_seconds(config, stage)` returns the stage value, or the documented 120 second default for a stage that registers none.
A timeout must be a whole number of seconds from 1 to 900.

`SharedGeminiBroker.execute` reads the stage timeout, writes it into the receipt identity as `timeout_seconds`, and builds `GeminiTransport` with it.
Every receipt of the call carries the field: the count event, the submitted sidecar, the final receipt and the ledger request row.
The evidence of item 1 can therefore name the timeout of a future timed-out call from the receipt itself.

Two fixed timeouts on generation-stage calls are gone.
`streaming.py:2581` now reads the eligibility stage timeout from the same config.
The `timeout: 30` value in `_generate_candidate_attempt` now reads the longest registered stage timeout from the broker config.
Both values are caller hints that `BrokerProvider.invoke` discards, because the broker owns the timeout for a broker call.
They are now no shorter than the stage they can reach.

### Effect on the activation half

The price config file changes, so its sha256 changes.
The activation for the landed commit must chain one `shared-paid-call-config-transition-v3` price transition from `cb9a3fd23cc335f4a46b6e190809277dc3787c804c5c981d26ceec196eb86cf8` to the new value, and record it.
The budget policy, the ceiling, the run id, the campaign and the frozen order do not change.

## 3. Test results

New tests in `tests/test_ambiguous_continuation.py`:

- `test_provider_timeout_continuation_retains_reserve_and_never_replays` - the timeout case is admitted with exact evidence, the stored event carries the new schema, error class, skip reason and `timeout_seconds` 300, the ledger leaves the halt, `ambiguous_reserved_usd` keeps the full reservation, `spent_usd` does not move, the family stays in custody, a replay is refused and an unrelated paper completes.
- `test_timeout_and_http_evidence_never_release_each_other` - 5xx evidence is refused for a timeout receipt, timeout evidence with the wrong `timeout_seconds` is refused, timeout evidence is refused for a 503 receipt, and each receipt is released only by its own exact evidence. The ledger stays halted and no event file is written after each refusal.
- `test_stage_call_timeout_comes_from_the_price_config` - the four judge stages report 300 seconds, the writer and judge stages report 120, and a completed receipt of each kind carries the matching `timeout_seconds`.

New test in `tests/test_gemini_eligibility.py`:

- `test_pro_judge_stages_carry_a_pinned_longer_call_timeout` - v7 pins 300 seconds on the judge stages, a shorter judge timeout is refused, a zero timeout is refused, and a v6 config that carries the judge timeout is refused.

Two existing tests asserted the old config id.
`tests/test_gemini_eligibility.py::test_config_requires_low_thinking_for_bounded_structured_output` and `tests/test_chapter2_integration.py::test_price_config_v6_pins_the_judge_model_and_its_price` now assert v7.
The second test also asserts the pinned 300 second judge timeout, and is renamed to `test_price_config_v7_pins_the_judge_model_and_its_price`.
No other test changed.

Whole suite, in four bounded foreground parts in the nix devshell, 807 tests collected:

```
nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_model_broker.py tests/test_gemini_eligibility.py -q'          -> all passed
nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_access_readiness.py ... tests/test_distractor_validation.py -q' -> all passed
nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_eligibility_geography_v7.py ... tests/test_project_progress_viewer.py -q' -> all passed
nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_publication_export.py ... tests/test_writer_context_bundle.py -q' -> all passed
```

`ruff check src/ tests/` and `ruff format --check src/ tests/` are clean.

## 4. The timeout decision

The bounded change was possible, so the timeout is not left alone.
The price config already carries the per-stage model facts that the broker reads through `model_config_for_stage`, and it is already bound into every receipt through `price_config_sha256`.
The roles file carries no price or limit, and the broker never reads it, so a timeout there would not be bound into the ledger.
The price config is therefore the correct carrier.

The default stays 120 seconds.
The four `gemini-3.1-pro-preview` stages get 300 seconds.
The upper bound is 900 seconds, which keeps a misconfiguration from stranding the single-concurrency producer.

The cost is one price config transition in the activation half.
The gain is that the Pro judge is no longer cut off mid-answer, and that every future timed-out receipt names its own timeout.
