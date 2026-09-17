#!/usr/bin/env bash
# Trials recorded per model since a given systemd-journal time, from the unit log.
since="$1"
journalctl --user -u arctic-abstention-stream-r3 --since "$since" --no-pager -o cat \
  | grep '"trial_id"' \
  | sed -n 's/.*"model":"\([^"]*\)".*/\1/p' \
  | sort | uniq -c | sort -rn
echo "--- states ---"
journalctl --user -u arctic-abstention-stream-r3 --since "$since" --no-pager -o cat \
  | grep '"trial_id"' \
  | sed -n 's/.*"state":"\([^"]*\)".*/\1/p' | sort | uniq -c
echo "--- 429 / 503 / retries ---"
journalctl --user -u arctic-abstention-stream-r3 --since "$since" --no-pager -o cat \
  | grep -oE "HTTP (429|4[0-9][0-9]|5[0-9][0-9])|429|503|RESOURCE_EXHAUSTED|UNAVAILABLE" \
  | sort | uniq -c | head
