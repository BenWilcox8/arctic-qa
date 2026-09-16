# Codex attribution and pause hysteresis of the benchmark cost guard

Task `arctic-benchmark-guard-codex-attribution-r1`, branch `fm/arctic-benchmark-guard-codex-attribution-r1`, 2026-09-16.

The reference document is `docs/BENCHMARK_GUARD.md`, sections "The Codex attribution" and "Hysteresis".
This report holds the fault, the decision and the live evidence.

## 1. The fault

The rule `codex_projected_exhaustion` paused and resumed `gpt-5.6-sol` on alternate five-minute cycles.
Between 18:20 and 18:55 UTC on 2026-09-16 the guard wrote five pauses and four resumes.

The rule fired when `quota-axi` projected the Codex weekly window exhausted before its reset, and `_benchmark_drives_codex` was true.
That second test only asked whether the evaluator had booked one more Codex call since the last cycle.
A scored question made that true, so the guard paused the model.
The paused model then booked no call, the test went false, and the guard resumed it.

The projection itself was correct but it did not belong to the benchmark.
The Codex weekly window sat at 22 percent and did not move a full percent in 40 minutes of benchmarking.
The projection came from other Codex sessions on this machine earlier that day.
The benchmark's own Codex cost is about USD 0.37 of list-price equivalent per question, over 18 calls.

Under the captain's order of 2026-09-16 that is not an urgent rise caused by the benchmark, so the guard must not pause it.

## 2. The decision

`_benchmark_drives_codex` is replaced by a measured attribution, `codex_attribution` in `src/arctic_qa/benchmark_guard.py`.

`quota-axi` reports no per-caller attribution, so the guard measures what it can see over a trailing window of at least 30 minutes:

- `window_percent_burn`, the `percent_remaining` delta of the Codex weekly window.
  The reported burn rate of `quota-axi` is an average over the whole elapsed weekly cycle, so it still carries the burn of earlier sessions.
  The guard takes that rate only after a window reset inside the trailing period.
- `benchmark_usd_burn`, the Codex list-price-equivalent USD of this benchmark over the same period, from the cost journal.
- `other_percent_burn`, the burn of the other sessions at the baseline rate of the intervals in which this benchmark spent nothing.

The rest of the burn is the benchmark's.
That gives the share and the window's percent-per-USD.
The rule fires when the share is one half or more, or when the benchmark's own extrapolated burn to the end of the run would exhaust the window before the reset.
The rule cannot fire while the trailing window or the idle baseline is incomplete, or while the whole account burned one percent point or less over the window.

The guard also holds every pause it owns.
It removes a pause only after three consecutive clear cycles, and it does not pause a model again by the same rule for 30 minutes after it resumed that model.
Both counters live in the new `guard-memory.json`, never in the pause file, whose captain-owned entries stay exactly as written.

## 3. The live evidence

The guard ran with `--once` against the live journal, ledger and policies, and against a copy of the live pause file under this worktree.

The first run started from an empty memory.
It took no Codex action and recorded `the trailing measurement window of 1800 seconds is not full yet`.

The second run started from a memory rebuilt from the live `guard-log.jsonl` poll rows and the live cost journal, which is 101 samples over eight hours.
`live-once-seeded-memory.json` holds its readings:

- measured over 2089 seconds, `window_percent_burn` 0.000000, `benchmark_usd_burn` 0.491460;
- `benchmark_share_of_window` 0.000000, against a floor of 0.5;
- `codex_projected_exhaustion` not fired, although the projection still stands before the reset;
- no action, and the copied pause file byte-identical to the live one, with the captain's `claude-fable-5-1` entry untouched.

`guard-execstart-before.txt` holds the command line of the live unit before this change, for the repoint after the merge.

## 4. What did not change

The Gemini rules, the Fable rule, the Claude session rule and the pause-file schema are unchanged.
The guard still makes no paid model call and still never stops a process.

## 5. The suite

`nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'` ran on this branch after the rebase onto `main` at `504ee68`.
All 1420 tests passed.
`tests/test_benchmark_guard.py` and `tests/test_live_benchmark_viewer.py` hold 46 of them.
`ruff check` and `ruff format --check` pass over `src` and `tests`.
