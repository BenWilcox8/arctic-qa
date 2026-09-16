# Evaluator cutover to snapshot `ea00336` and work directory `streaming-r11`

This is a prepared procedure, not a performed one.
Firstmate's decision of 2026-09-16 19:10 UTC is that the running unit finishes every item it has started, including the `claude-fable-5-1` trials that resume after 23:00 UTC, and that firstmate performs the cutover after that.
Everything the cutover needs is built and checked in.
Nothing of the evaluator was stopped, started or repointed by this task.

## 1. Why a cutover is needed at all

The evaluator writes one immutable plan manifest per item, at `<work-dir>/runs/<item_id>/plan-manifest.json`, and that manifest holds the code commit.
`abstention_plan.py` compares a new manifest with the stored one field by field and refuses a difference with "the run directory holds a different plan manifest".
The streaming authorization binds the same commit, and `abstention_watch.validate_authorization` refuses a mismatch.

So the code commit, the run id prefix and the work directory move together.
Section 8.2 of `data/arctic-abstention-streaming-eval-r1/report.md` states the same rule.

## 2. What is prepared

All paths are under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-broker-operation-lock-wait-r1/`.

| Artifact | Path | sha256 |
|---|---|---|
| Runtime snapshot | `runtime/app-ea00336-arctic-abstention-stream-r4` | archive `1eb051f7f5753abc86dab023c2470d1e93cf4c12c9d3a35dde2ebb7b0312843e` |
| Source archive | `source-ea00336-arctic-abstention-stream-r4.tar` | as above |
| Review record | `streaming-eval-r7-review.md` | `1fccde9d1305f02552ef4a31fca89195c5850402c4873a71cf14a98e491b8b33` |
| Authorization, signed | `streaming-eval-r7-authorization.json` | `264bebec89a3555cf346f5f587c582731e24332516de85a41bedd17a78a7c28b` |
| Launcher | `streaming-eval-r7-launcher.sh` | |
| **Cutover script** | **`cutover-evaluator-r11.sh`** | |

| Binding | Old | New |
|---|---|---|
| Code commit | `a0b9a82` | `ea00336` |
| Snapshot | `arctic-abstention-streaming-eval-r1/runtime/app-a0b9a82-…` | `runtime/app-ea00336-arctic-abstention-stream-r4` |
| Run id prefix | `abstention-stream-r10` | `abstention-stream-r11` |
| Work directory | `abstention-eval/streaming-r10` | `abstention-eval/streaming-r11` |
| Subscription ledger root | `abstention-eval/subscription-streaming-r10` | `abstention-eval/subscription-streaming-r11` |
| Authorization | `private/streaming-eval-r6-authorization.json` | `streaming-eval-r7-authorization.json` |
| Everything else | unchanged | unchanged |

The new launcher differs from the r6 launcher in those four values and in nothing else.
Every evaluation config file of the new snapshot, the plan, the contract, the policy, the prices, the subscription models, the list prices and the pause file, is byte-identical to the file of snapshot `a0b9a82`.
So every gate the r6 run derived stays valid under the new snapshot, and the authorization binds the same hashes.

The unit name stays `arctic-abstention-stream-r3`, because the instruction is to restart that unit.
A transient `systemd-run` name is free again once the unit is stopped.

## 3. The cost journal is carried forward

`cutover-evaluator-r11.sh` copies `cost-journal.jsonl` from `streaming-r10` into `streaming-r11` before it starts the new unit.

The journal is the evaluator's memory of which questions are evaluated and what they cost.
`pending_item_ids` reads the accepted items from the state database and excludes only what the current work directory's journal knows.
Without the copy the new run re-evaluates every question the r6 run finished and pays for the Gemini trials a second time.

The copied rows keep their own `run_id`, `abstention-stream-r10-<item>`, so no row claims to belong to the new run and every total stays true.

The copy is history, not headroom.
`remaining_bound` subtracts the carried item rows from the item bound of the authorization.
The prepared authorization keeps the r6 bounds, 12 items and USD 3.00, so with ten carried items the new run evaluates two new questions.
To let it take more, raise `maximum_items` before the cutover and sign the authorization again:

```bash
D=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-broker-operation-lock-wait-r1
APP=$D/runtime/app-ea00336-arctic-abstention-stream-r4
cd "$APP" && nix develop "path:$APP" -c env PYTHONPATH=$APP/src python -m arctic_qa --json abstention-eval \
  --action watch-authorization \
  --state-db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
  --campaign-id arctic-qa-production-campaign-003 \
  --run-id-prefix abstention-stream-r11 \
  --plan-file config/benchmark-evaluation-plan-high-v1.json \
  --contract-file config/live-dataset-current-contract-v1.json \
  --evaluation-policy-file config/benchmark-evaluation-policy-v3.json \
  --evaluation-price-config-file config/benchmark-evaluation-prices-v1.json \
  --subscription-models-file config/benchmark-evaluation-subscription-models-v1.json \
  --maximum-items <N> --maximum-gemini-usd <USD> \
  --code-commit ea00336 \
  --review-record $D/streaming-eval-r7-review.md \
  --output-file $D/streaming-eval-r7-authorization.json
```

The command writes the template with `independent_review_verdict` pending.
A reviewer then sets `independent_review_verdict` to `pass` and `authorization_enabled` to `true`, and updates the bound line of the review record.
The cutover script refuses an unsigned authorization and a review record whose sha256 no longer matches.

## 4. Preconditions

The script checks every one of these and refuses with exit 2 and a named reason.
They are also the conditions a human should read before running it.

1. The snapshot, the signed authorization and the launcher exist, and the review record still matches its recorded sha256.
2. `streaming-r11` does not exist yet, so a cutover cannot run twice.
3. **No pending paused trials.** No model is held by either pause file, under the evaluator's own rule in `abstention_plan.paused_models`: a model is held when it has no `resume_at_utc`, or when its `resume_at_utc` is still ahead. An entry whose resume time has passed holds nothing and may stay in the file.
4. **Every item of the old journal is complete.** The last row of each item has `complete` true, so no item is waiting for missing trials.
5. No vendor is paused in the old `watch-state.json`.
6. **No item is in progress.** The watcher state is newer than its newest journal row, which means the watcher polled after its last item and found nothing to do.
7. The shared ledger has `halted` false and `evaluation_halted` false, and no `benchmark_evaluation` request is `submitted`.

Today, 2026-09-16 19:15 UTC, the dry run refuses at condition 3: `claude-fable-5-1` is held until 23:00 UTC, and eleven items are waiting for its trials.

## 5. The procedure

```bash
D=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-broker-operation-lock-wait-r1
$D/cutover-evaluator-r11.sh            # dry run: check and print, change nothing
$D/cutover-evaluator-r11.sh --apply    # do it
```

The dry run is the default, so a bare run is always safe.
With `--apply` the script, in order:

1. Checks every precondition of section 4 and stops on the first failure.
2. Stops `arctic-abstention-stream-r3`.
3. Creates `streaming-r11` and `subscription-streaming-r11`, and copies the cost journal forward.
4. Starts `arctic-abstention-stream-r3` again with `systemd-run --user`, on the new snapshot as working directory and the new launcher.
5. Repoints the cost guard `arctic-benchmark-guard-r1`, `--journal-dir`, to `streaming-r11`.
6. Repoints the viewer `arctic-corpus-stage-r1-formatting`, `--benchmark-journal-dir`, to `streaming-r11`.
7. Waits up to ten minutes for `streaming-r11/watch-state.json` to show at least one poll, prints it, and writes `cutover-receipt-<stamp>.json`.

Both the guard and the viewer are transient `systemd-run` units, so a repoint is a stop and a start with one argument changed.
The script reads each unit's current argument vector from systemd and replaces only the value after the named flag, so no other argument can drift.
That rewrite was tested against both live units: the argument count is unchanged, exactly one `streaming-r10` becomes `streaming-r11`, and the viewer's one argument that contains spaces survives as a single token.

## 6. If the cutover goes wrong

Stop the unit, delete `streaming-r11` and `subscription-streaming-r11`, and start the r6 launcher again:

```bash
systemctl --user stop arctic-abstention-stream-r3
rm -rf /mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11 \
       /mnt/crdata/research-abstention/arctic-qa/abstention-eval/subscription-streaming-r11
A=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-abstention-streaming-eval-r1/runtime/app-a0b9a82-arctic-abstention-streaming-eval-r1
systemd-run --user --unit=arctic-abstention-stream-r3 --working-directory=$A \
  /mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/streaming-eval-r6-launcher.sh
```

The old work directory is untouched by the cutover, so the r6 run resumes exactly where it stopped.
Repoint the guard and the viewer back the same way, with `streaming-r10`.

## 7. An ambiguous charge before the cutover

**Yes, the release command of the new snapshot can act on the old work directory.**

`authorize-ambiguous-continuation` acts on the shared ledger and the model receipts.
Both units share those, and a work directory holds no ledger state, so the snapshot that runs the release does not have to be the snapshot that made the charge.
The work directory contributes one file only, the derived per-item evaluation gate, and the command takes it as an argument.

Two bindings must be right, and both point at the **old** work directory.

1. `--evaluation-gate-file` must be the gate the charged request ran under, `streaming-r10/gates/<item_id>/<vendor>.json`. Do not regenerate it. Since `d64e8c4`, an evaluation-phase release reads the evaluation gate rather than the construction gate, and compares the gate's `authorized_run_id` with the `--authorized-run-id` argument.
2. `--authorized-run-id` must be the r10 run id of that item, `abstention-stream-r10-<item_id>`.

Every other argument may come from the new snapshot, because the evaluation policy, the evaluation prices and the construction files are byte-identical to the ones the gate binds.
Section 11 of `data/arctic-eval-503-release-r1/report.md` holds the full command; only `$APP` changes to `runtime/app-ea00336-arctic-abstention-stream-r4` of this activation directory.

One caution: the release takes the ledger's exclusive operation lock without waiting, as every reviewed operation does.
Since commit `f53e3e2` that no longer risks the producer, because the producer's request waits 120 seconds for the lock instead of ending the run.

## 8. What firstmate should report after the cutover

The snapshot path, `runtime/app-ea00336-arctic-abstention-stream-r4`, the work directory `streaming-r11`, and the minute the unit was restarted, which the receipt records as `cut_over_at_utc`.
