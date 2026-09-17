#!/usr/bin/env bash
# Prove the three answers of eval-finished.sh against small fixtures: the
# question that is whole, the question that still owes an askable trial, and
# the question the re-open rule cannot clear.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

build() {            # build <work-dir> <recorded-trials-of-item-A>
  local work="$1" recorded="$2" dir
  dir="$work/runs/aqa-whole/google_gemini"
  mkdir -p "$dir"
  : > "$dir/trials.jsonl"
  : > "$dir/responses.jsonl"
  for i in $(seq 1 48); do
    echo "{\"trial_id\":\"t$i\",\"model\":\"gemini-3.8-flash\"}" >> "$dir/trials.jsonl"
  done
  for i in $(seq 1 "$recorded"); do
    echo "{\"trial_id\":\"t$i\",\"model\":\"gemini-3.8-flash\",\"response\":{\"state\":\"completed\"}}" \
      >> "$dir/responses.jsonl"
  done
  cat > "$work/cost-journal.jsonl" <<JSON
{"kind":"item","item_id":"aqa-whole","run_id":"abstention-stream-r11-aqa-whole","evaluation":{"recorded_trials":$recorded,"planned_trials":48}}
JSON
}

db() {               # db <file> <item-ids...>
  python "$HERE/make-fixture-db.py" "$@"
}

check() {            # check <name> <work> <db> <expected-lines> [<grep>]
  local name="$1" work="$2" state="$3" want="$4" needle="${5:-}" out lines
  out="$("$HERE/eval-finished.sh" "$work" "$state")"
  lines=$([ -z "$out" ] && echo 0 || printf '%s\n' "$out" | wc -l)
  if [ "$lines" != "$want" ]; then
    echo "FAIL $name: expected $want line(s), got $lines: $out" >&2
    exit 1
  fi
  if [ -n "$needle" ] && ! printf '%s' "$out" | grep -q -- "$needle"; then
    echo "FAIL $name: line does not name $needle: $out" >&2
    exit 1
  fi
  echo "ok   $name${out:+ -> $out}"
}

# 1. Every accepted question is whole: one line.
W="$ROOT/whole"; mkdir -p "$W"; build "$W" 48
db "$ROOT/whole.sqlite3" aqa-whole
check "whole" "$W" "$ROOT/whole.sqlite3" 1 "every one of the 1 accepted questions"

# 2. A question still owes an askable trial: nothing at all.
W="$ROOT/open"; mkdir -p "$W"; build "$W" 40
db "$ROOT/open.sqlite3" aqa-whole
check "still open" "$W" "$ROOT/open.sqlite3" 0

# 3. A question whose run directory belongs to another run id: one line
#    that names it, because the re-open rule cannot clear it.
W="$ROOT/blocked"; mkdir -p "$W"; build "$W" 48
cat >> "$W/cost-journal.jsonl" <<'JSON'
{"kind":"item","item_id":"aqa-elsewhere","run_id":"abstention-stream-r10-aqa-elsewhere","evaluation":{"recorded_trials":40,"planned_trials":48}}
JSON
db "$ROOT/blocked.sqlite3" aqa-whole aqa-elsewhere
check "unrecoverable" "$W" "$ROOT/blocked.sqlite3" 1 "abstention-stream-r10"

# 4. An accepted question with no journal row at all: nothing, because the
#    evaluator has never seen it.
db "$ROOT/unknown.sqlite3" aqa-whole aqa-new
check "never seen" "$ROOT/whole" "$ROOT/unknown.sqlite3" 0

echo "all four answers hold"
