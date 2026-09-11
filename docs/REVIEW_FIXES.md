# Independent review corrections

This file maps each failed review control to its correction and executable test.
The reviewed commit is `f86cbcd2ff4ccb209ac9a74db714e42ff15c2d64`.
The correction commit is separate.

| Review finding | Corrected behavior | Counterexample and valid control |
| --- | --- | --- |
| Failed source entailment could pass | Validation returns `unresolved` when independent source entailment is false or absent. | `test_validation_requires_positive_source_entailment` |
| Distractor rules trusted self-asserted values | Nonnumeric rules now come from the source-located answer. The displayed option must parse to the compared value. Numeric values, units, and tolerance must occur in the source and displayed answer. | `test_self_asserted_typed_distractor_rules_are_not_deterministic`, `test_source_bound_typed_distractor_controls`, and `test_numeric_rule_must_match_source_and_displayed_answer` |
| Geography trusted a naked JSON label | Screening requires a stored source hash, exact chunk quote, offsets, and complete-site language. Every reported latitude and named region must occur in the quote. It validates latitude range and computes mixed scope. | `test_geography_requires_valid_source_bound_complete_site_evidence` |
| A small reservation could overspend | The reservation is now the larger of the user floor and a computed request bound. The CLI reserves every retry before dispatch. USD mode requires configured prices. | `test_tiny_user_reservation_cannot_bypass_full_request_bound`, `test_unexpected_provider_overage_is_recorded_and_stops_run`, and `test_usd_budget_requires_pricing_before_dispatch` |
| Provider schemas checked only top-level keys | Each role now sends and validates its nested object, array, scalar, and required-field contract. A malformed nested response becomes a structured provider error. | `test_nested_provider_contract_is_checked_before_completion` |
| Fetch accepted local file URLs | Production retrieval now accepts HTTPS only. Redirect targets and final URLs use the same check. A local fixture needs both `--test-mode` and `--allow-test-file`. | `test_fetch_rejects_local_urls_without_explicit_test_fixture_mode` |

These tests use synthetic, test-only inputs.
They verify software controls only.
They do not establish scientific truth or model quality.
