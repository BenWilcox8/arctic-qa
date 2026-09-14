# Corrected production segment brief

The campaign remains `arctic-qa-production-campaign-001` on the original
ranked-800 input and shared ledger. It retains the USD11.614496 historical
baseline, USD50 new-spend limit, USD61.614496 cumulative cap, USD0.25 request
cap, USD1 family cap, no fallback, and no automatic retries.

The first v3 attempt preserved its immutable manifest at
`stream-invocation-021795104ae043a532e5/run-manifest.json`, SHA-256
`1f0dc22f6a8fc304978e2f26fda60ebee1a014a8fe34c20a59f07e21aca2d669`.
It refused before provider transport because it was bound to v2 inputs.

The replacement segment is `first-production-6fbdf41-r1-geo-v3` with separate
v3 eligibility records and manifest
`stream-invocation-6107660d3e0758844998/run-manifest.json`, SHA-256
`4a4385f0d6932061e0f3f60c6ee86476e52202a49afca40c40f0c48b3e3e0c4e`.
Its original v3 gate was
`gate-6fbdf41-production-50usd-r1-v3-geo-segment-r1.json`, SHA-256
`3801fe6312eb282329a15aba235ebce9513b2f1accf1d75d8c6a9e39fabcdfe7`.
That gate is not used for the pending resume because code changed after the
initial release.

The v3 policy, prompt, and schema SHA-256 values are respectively
`64bd4d99527b96c1b52e1dbc91c3b02a3c7c58610e07770ea40fa8f90f65a773`,
`a2bdcedf4e75545ec7af8ecc46934a09671e6d5df986c4220d66445b1bf07d4e`, and
`8030c29549500f63aebe351a2cfbe61bafe790324e2833628e714f30aa8f92ef`.

The 10-decision live sample contained three valid eligible records and seven
uncertain records. Six uncertain records had satisfied geography but failed
deterministic activity/phrase scope binding, including strong northern cases;
they remain uncertain and are not autoaccepted. One secondary synthesis had
scientifically unresolved geography plus a missing-context contract failure.
No v14 verifier-field mismatch appeared in this sample.

The initial stop was a fin-whale scope quote that differed from a generation
chunk only by terminal whitespace. Commit
`a4f59aec5413b8ff4b16db78d698c9d2240bd21f` permits only exact-token,
whitespace-equivalent matching, records both eligibility and chunk hashes, and
rejects altered number, unit, negation, or other word changes. Commit
`0b77b53dd9a59d9e44b1365221353b950efffa97` makes a remaining unbound scope
span a paper-local generation rejection, without catching global integrity,
budget, ledger, or provider errors. Focused tests and lint pass. A narrow
review recheck is required before an immutable successor gate binds this code.
