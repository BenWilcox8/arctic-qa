# Review: provider rejection 585436686ba860d2a789530888a8a8d15d11045d7489df09d62f53344190d680

Reviewer: arctic-ch3-production-run-r1 crewmate (claude-fable-5-1), firstmate decision 2026-09-16T07:30Z.
Reviewed at: 2026-09-16T08:02:28Z.

## Evidence read

- Receipt `/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts/585436686ba860d2a789530888a8a8d15d11045d7489df09d62f53344190d680.json`, its `.submitted.json` and `.request-trace.json`.
- Run `chapter3-7dc6485-r1`, campaign `arctic-qa-production-campaign-003`, stage `eligibility`, model `gemini-3.8-flash`, paper `10.37482/issn2221-2698.2025.59.44`, family `family-4a182d987f95fb00f5ed`.
- `http_status` 400, `error_class` `known_http_response_unknown_charge`, `live_call_made` true, no response, no received receipt, reserved USD 0.048194, `countTokens` accepted 23,298 input tokens.
- The receipt predates the error-body capture, so it holds no provider message.
- One authorized diagnostic call (firstmate decision 2026-09-16 07:30 UTC, step 1) sent the exact traced payload (request sha256 `f8e078e8ad6951cc982528cf26f6878137cd20e19c27116242b3183c6fa21ecd`) at 2026-09-16T07:30:55.454684+00:00 and received HTTP 400 `INVALID_ARGUMENT`: "Request contains an invalid argument.". Record: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1/diagnostic-400-call.json`, sha256 `96eecb5290e721300f23df754db97ea9ec109e516f7aeb090d2b0032f4805915`.
- Four hypothesis calls with the same payload isolated the cause: an enum inside the items of the reason_codes array of eligibility schema v4. The same request with that enum removed returned 200. Record: `diagnostic-hypothesis-calls.json`.

## Finding

The provider rejected the request before generation. A rejection is not billed. The charge is known: zero.
The cause is inside the eligibility request and is fixed in the runtime (commit named in the report), with a test that keeps every live eligibility schema inside the accepted keyword set.

## Decision

Settle request `585436686ba860d2a789530888a8a8d15d11045d7489df09d62f53344190d680` at zero cost: release the reservation of USD 0.048194 from the ambiguous funds and lift the halt.
The request is never replayed: its key stays a settled record. The corrected eligibility request for the same paper has a new key.
The family `family-4a182d987f95fb00f5ed` is not skipped; paper 1 is screened by the corrected request in frozen order.
