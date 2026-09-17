#!/usr/bin/env bash
# Put the streaming abstention evaluator on a new snapshot of arctic-qa main.
#
#   resnapshot.sh <short-sha>            # dry run: check and print, change nothing
#   resnapshot.sh <short-sha> --apply    # do it
#
# It makes the runtime snapshot, the successor authorization and the launcher,
# then stops the unit at a settled boundary and starts it again. It makes no
# paid call of its own and touches no pause file.
set -euo pipefail

SHA=${1:?a short commit sha of arctic-qa main}
APPLY=0
[[ "${2:-}" == "--apply" ]] && APPLY=1

D=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8
E=/mnt/crdata/research-abstention/arctic-qa/abstention-eval
REPO=${ARCTIC_QA_REPO:?the arctic-qa worktree that holds the commit}
LEDGER=/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
UNIT=arctic-abstention-stream-r3
STATEPY=${ARCTIC_LEDGER_STATE_PY:?the ledger-state reader}
PREV=09314ae
APP=$D/runtime/app-$SHA-arctic-eval-authorization-r8
AUTH=$D/streaming-eval-r11-authorization-$SHA.json
REVIEW=$D/streaming-eval-r11-review-$SHA.md
LAUNCH=$D/streaming-eval-r11-launcher-$SHA.sh

say() { printf '%s\n' "$*"; }
act() { if (( APPLY )); then say "RUN  $*"; "$@"; else say "SKIP $*"; fi; }

say "== preconditions =="
[[ -f "$D/streaming-eval-r11-authorization-$PREV.json" ]] || { say "FAIL: no predecessor authorization"; exit 2; }
[[ -f "$REVIEW" ]] || { say "FAIL: write the review record $REVIEW first"; exit 2; }
git -C "$REPO" rev-parse --verify "$SHA^{commit}" >/dev/null || { say "FAIL: $SHA is not a commit"; exit 2; }
halt=$(dirname "$LEDGER")/.$(basename "$LEDGER").integrity-halt.json
[[ ! -e "$halt" ]] || { say "FAIL: the shared ledger has an integrity halt"; exit 2; }
# The JSON file is the compacted snapshot, not the ledger: read the store.
state() {
  nice -n 10 nix develop "path:$REPO" -c env PYTHONPATH="$REPO/src" \
    python "$STATEPY" "$LEDGER"
}
now=$(state)
say "  ledger: $now"
[[ "$(jq -r .evaluation_halted <<<"$now")" == "false" ]] \
  || { say "FAIL: the shared ledger halts the evaluation phase"; exit 2; }
# A construction halt belongs to the producer and its crew, not to this
# cut-over: the evaluation phase has its own halt and its own release.
[[ "$(jq -r .halted <<<"$now")" == "false" ]] \
  || say "  note: the construction phase is halted ($(jq -r .halt_reason <<<"$now")); the evaluation phase is not"

say "== the snapshot =="
act bash -c "git -C '$REPO' archive --format=tar --prefix=app-$SHA-arctic-eval-authorization-r8/ '$SHA' > '$D/source-$SHA-arctic-eval-authorization-r8.tar'"
act bash -c "rm -rf '$APP' && mkdir -p '$D/runtime' && tar -xf '$D/source-$SHA-arctic-eval-authorization-r8.tar' -C '$D/runtime'"

say "== the successor authorization =="
if (( APPLY )); then
  jq -n \
    --slurpfile prev "$D/streaming-eval-r11-authorization-$PREV.json" \
    --arg sha "$SHA" --arg review "$REVIEW" \
    --arg review_sha "$(sha256sum "$REVIEW" | cut -d' ' -f1)" \
    --arg prev_file "$D/streaming-eval-r11-authorization-$PREV.json" \
    --arg prev_sha "$(sha256sum "$D/streaming-eval-r11-authorization-$PREV.json" | cut -d' ' -f1)" \
    --arg prev_commit "$PREV" \
    --arg prev_review "$(jq -r .review_record "$D/streaming-eval-r11-authorization-$PREV.json")" \
    --arg prev_review_sha "$(jq -r .review_record_sha256 "$D/streaming-eval-r11-authorization-$PREV.json")" \
    --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '$prev[0]
     | .integrated_code_commit = $sha
     | .review_record = $review
     | .review_record_sha256 = $review_sha
     | .supersedes = {authorization_file:$prev_file, authorization_sha256:$prev_sha,
                      integrated_code_commit:$prev_commit,
                      review_record:$prev_review, review_record_sha256:$prev_review_sha}
     | .written_at_utc = $at' > "$AUTH"
  say "  wrote $AUTH"
else
  say "SKIP write $AUTH"
fi

say "== the launcher =="
# The measurement threshold of the exclusive operation lock is 1.0 s by
# default, and no section reached it in the 17 minutes before this cut-over.
# The new launcher lowers it, so the hold of every section is measurable from
# the unit's own log. It changes what is printed and nothing else.
act bash -c "sed -e 's/$PREV/$SHA/g' \
  -e 's|-c env PYTHONPATH=|-c env ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS=0.05 PYTHONPATH=|' \
  '$D/streaming-eval-r11-launcher-$PREV.sh' > '$LAUNCH' && chmod +x '$LAUNCH'"

say "== the settled stop =="
# A cut-over while a Gemini call is on the wire orphans the call and halts the
# evaluation phase: that happened at 10:03Z on 2026-09-17. So the unit stops
# only when this evaluator holds no submitted request of the shared ledger.
if systemctl --user is-active --quiet "$UNIT"; then
  for i in $(seq 1 120); do
    inflight=$(jq -r .evaluation_inflight <<<"$(state)")
    [[ "$inflight" == "0" ]] && break
    say "  evaluation requests in flight: $inflight (wait $i)"
    sleep 5
  done
  [[ "$inflight" == "0" ]] || { say "FAIL: $inflight evaluation requests still in flight"; exit 2; }
  say "  inflight 0: the stop is settled"
  # The running unit carries the 90 s default TimeoutStopSec, and that default
  # killed the unit mid-trial on 2026-09-17 at 07:25:55Z. So the signal goes
  # to the process and this waits for it, rather than let systemd kill it.
  # SIGTERM sets a flag that the evaluator reads before it dispatches each
  # trial, so the stop lands at a trial boundary.
  main=$(systemctl --user show "$UNIT" -p MainPID --value)
  say "  main pid $main"
  if (( APPLY )) && [[ -n "$main" && "$main" != "0" ]]; then
    kill -TERM "$main"
    for i in $(seq 1 120); do
      kill -0 "$main" 2>/dev/null || break
      say "  waiting for pid $main to leave ($((i * 5)) s)"
      sleep 5
    done
    if kill -0 "$main" 2>/dev/null; then
      say "FAIL: pid $main did not leave; do not kill it, it may hold a paid call"
      exit 2
    fi
    say "  the evaluator left at a trial boundary"
  else
    say "SKIP kill -TERM $main"
  fi
  # The evaluator that leaves of its own accord takes its transient unit with
  # it, and systemd then refuses to stop a unit it no longer holds. That is
  # the settled stop working, not a fault.
  act systemctl --user stop "$UNIT" || say "  the unit left with its process"
else
  say "  the unit is not running"
fi
if [[ "$(systemctl --user is-failed "$UNIT" || true)" == "failed" ]]; then
  act systemctl --user reset-failed "$UNIT"
fi

say "== the start =="
# nice 10: the machine carries the 75-worker producer as well.
# TimeoutStopSec 600: a stop must land at a trial boundary, and the 90 s
# default killed the unit mid-trial on 2026-09-17 at 07:25:55Z.
act systemd-run --user --unit="$UNIT" --working-directory="$APP" \
  --nice=10 -p TimeoutStopSec=600 "$LAUNCH"

if (( APPLY )); then
  say "== prove it polls =="
  for _ in $(seq 1 40); do
    sleep 15
    systemctl --user is-active --quiet "$UNIT" || { say "FAIL: the unit is not active"; exit 2; }
    journalctl --user -u "$UNIT" --no-pager -o cat -n 200 | grep -q "item_started\|poll" && break
  done
  systemctl --user show "$UNIT" -p ActiveState -p Nice -p TimeoutStopUSec --value | tr '\n' ' '
  say ""
fi
