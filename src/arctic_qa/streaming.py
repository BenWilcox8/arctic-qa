from __future__ import annotations

import json
import inspect
import re
from decimal import Decimal
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Callable

from . import generation as generation_contract
from . import validation as validation_contract
from .db import Database, now
from .discovery import manual_record
from .errors import (
    AmbiguousChargeError,
    BrokerOperationBusyError,
    BudgetError,
    CandidateRejectedError,
    PaperCostCapError,
    ProviderResponseError,
    is_run_stop,
    mark_run_stop,
)
from .exporting import export_run
from .extraction import extract_source
from .generation import generate_candidate
from .gemini_eligibility import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    ELIGIBILITY_STATUS_MAPPING_VERSION,
    MAXIMUM_FORMAT_ATTEMPTS,
    SCOPE_CONTRACT_VERSIONS,
    SPAN_CONTRACT_VERSIONS,
    UNRESOLVED_STATE,
    _correction_metadata,
    _job_key,
    _known_context_gaps,
    _persist_span_manifest_v2,
    _repair_moved_a_status,
    _repair_note,
    _request_payload,
    _span_blocks_v2,
    _span_manifest_v2,
    _validation_evidence,
    call_timeout_seconds,
    format_repairable,
    geography_rescreen_eligible,
    maximum_call_timeout_seconds,
    repaired_phrases_are_specific,
    shadow_two_pass_measurement,
    validate_response,
)
from .providers import Provider, call_provider, provider_model
from .model_broker import PER_REQUEST_CAP_REASON, broker_request_key
from . import paper_completion
from .model_roles import (
    JUDGE_ROLES,
    MODEL_ROLES_CONTRACT_VERSION,
    WRITER_ROLE,
    assert_profile_allowed_for_phase,
    assert_role_separation,
    load_role_contract,
    resolve_roles,
    role_separation_error,
)
from .storage import store_original
from .util import atomic_json, canonical_json, sha256_bytes, sha256_file, stable_id
from .validation import validate_candidate


GENERATION_ATTEMPT_CONTRACT_VERSION = (
    generation_contract.GENERATION_ATTEMPT_CONTRACT_VERSION
)
MAX_FINDING_ATTEMPTS = 2
MAX_QUESTION_REVISIONS = 2
MAX_CANDIDATE_PATHS = MAX_FINDING_ATTEMPTS * (MAX_QUESTION_REVISIONS + 1)
REPAIRABLE_QUESTION_REASONS = frozenset(
    {
        "answer_ambiguous",
        "alternative_answer_unresolved",
        "causal_overclaim",
        "question_context_invalid",
        "question_context_missing",
        "question_context_unnecessary",
        "question_context_not_source_supported",
        "question_context_answer_leakage",
        "question_context_required",
        "question_context_referent_unresolved",
        "question_answer_leakage",
        "benchmark_text_malformed",
        "publication_relative_period",
        "question_qualifier_not_evidence_bound",
        "scope_value_not_source_supported",
        # audit 4.6 d: the three frozen-record scope codes reach the rebind rung
        "answer_scope_not_source_bound",
        "answer_verifier_scope_not_source_bound",
        "reconstruction_scope_not_source_bound",
        # reconstruction-record-v2 sibling (gates slice, yield audit 4.3 f): a
        # rewrite can restate the displayed scope, so it earns the same rung.
        "reconstruction_scope_contradicts_answer",
        "revision_unchanged_payload",
        "question_claim_type_disagreement",
        "reconstruction_disagreement",
        "relation_scope_mismatch",
        "scope_qualifier_missing",
        "scope_qualifier_not_displayed",
        "scope_qualifier_not_source_bound",
        "benchmark_text_raw_source_artifact",
        "interpretation_scope_not_applicable_to_finding",
        "source_entailment_not_verified",
        "insufficient_verified_distractors",
        "standalone_gate_failed",
        "standalone_verification_unresolved",
        "standalone_undefined_subject_or_system",
        "standalone_undefined_measured_variable",
        "standalone_undefined_unit_meaning",
        "standalone_undefined_percentage_basis",
        "standalone_undefined_acronym",
        "standalone_undefined_location",
        "standalone_undefined_period_or_event",
        "standalone_undefined_population_or_sample",
        "standalone_undefined_treatment_or_condition",
        "standalone_undefined_comparison_basis",
        "standalone_unresolved_study_local_referent",
        "standalone_source_dependent_locator",
        "standalone_answer_leakage",
        "standalone_multiple_interpretations",
        "standalone_malformed_text",
        # ch2 yield audit 4.2 (F7 part 2): the free deterministic screen now
        # speaks in its own namespace. Same repair as the unprefixed codes.
        "standalone_det_question_context_missing",
        "standalone_det_question_context_referent_unresolved",
        "standalone_det_benchmark_text_malformed",
        "standalone_det_source_dependent_locator",
        "standalone_det_publication_relative_period",
        # ch2 yield audit 4.8: the whole-set verdict codes earn an option repair.
        "option_set_not_mutually_exclusive",
        "option_set_answer_not_choosable",
        # Cost slice (audit 4.4): the writer's own slot record said the source
        # does not state a slot, so no judge was called. Routed like the
        # matching standalone_undefined_* code; the routing slice owns the logic.
        *generation_contract.WRITER_SLOT_UNAVAILABLE_REASONS,
    }
)
# Cost slice (audit 4.9 C2): the eligible finding context exceeded the
# measured payload budget before any paid call. Terminal, contract layer.
TERMINAL_GENERATION_REASONS = frozenset({"finding_context_over_budget"})
ALTERNATIVE_FINDING_REASONS = frozenset(
    {
        # Cost slice (audit 4.5 b, c): freeze-time codes that leave the finding.
        "no_admissible_finding",
        "finding_required_phrase_artifact",
        "finding_answer_phrase_in_required_question_phrases",
        "finding_evidence_quote_excludes_finding",
        "insufficient_verified_distractors",
        # ch2 yield audit 4.2: an unevidenced judge verdict is a contract
        # violation, so routing moves to another finding, never a revision.
        "standalone_verdict_unevidenced",
        # ch2 yield audit 4.8: the option stage had no legal move on this
        # finding; no rewrite of the question changes that.
        "option_pool_empty_after_prefilter",
        "closed_set_closure_not_source_established",
        "reconstruction_disagreement",
        "eligible_arctic_scope_missing_from_finding",
        "eligible_arctic_finding_out_of_scope",
        "finding_evidence_components_not_contiguous",
        "reconstruction_alternative_answer_present",
        "revision_unchanged_payload",
        "slot_evidence_unavailable",
        "finding_span_is_table_or_caption",
        "finding_span_figure_defined_referent",
        "interpretation_span_contains_answer",
        # Chapter 3 writer-context slice: a frozen scope value with no cited span.
        "finding_scope_value_unsourced",
    }
)
IMMEDIATE_ALTERNATIVE_FINDING_REASONS = frozenset(
    {
        # Cost slice (audit 4.5 b, c): registered beside the other admission codes.
        "no_admissible_finding",
        "finding_required_phrase_artifact",
        "finding_answer_phrase_in_required_question_phrases",
        "finding_evidence_quote_excludes_finding",
        # ch2 yield audit 4.2 and 4.8: see ALTERNATIVE_FINDING_REASONS.
        "standalone_verdict_unevidenced",
        "option_pool_empty_after_prefilter",
        "closed_set_closure_not_source_established",
        "eligible_arctic_scope_missing_from_finding",
        "eligible_arctic_finding_out_of_scope",
        "finding_evidence_components_not_contiguous",
        "revision_unchanged_payload",
        "slot_evidence_unavailable",
        "finding_span_is_table_or_caption",
        "finding_span_figure_defined_referent",
        "interpretation_span_contains_answer",
        # Chapter 3 writer-context slice: a freeze-time reject, no writer call
        # can repair it, so the family moves to the next finding at once.
        "finding_scope_value_unsourced",
    }
)
# ch2 yield audit 4.8: a failed whole-set verdict regenerates the option set on
# the verified question, so it earns the same rung as a distractor shortfall.
# The generation contract owns the closed set and routing reads it, because a
# trigger this layer accepts and the contract refuses ends the producer.
OPTION_REPAIR_REASONS = generation_contract.OPTION_REPAIR_TRIGGER_REASONS
# Eligibility contract codes. They end a screening attempt before any candidate
# exists, so no candidate-level rung can repair them; the eligibility stage has
# its own bounded re-ask. They are registered here so the routing layer owns one
# entry per code (chapter 3 slice arctic-ch3-eligibility-r1, audit 4.7).
ELIGIBILITY_CONTRACT_REASONS = frozenset(
    {
        # The activity spans carry no dimension their own text states.
        "eligible_arctic_scope_dimension_unsupported",
        # A repaired scope phrase binds but names no station, region or stratum.
        "eligible_arctic_scope_phrase_not_specific",
    }
)
ANSWER_RULE_REPAIR_REASONS = frozenset({"source_bound_numeric_rule_missing"})
# One scope family, one failure layer, one pair of rungs (chapter 2 yield audit
# 4.6 d, finding R3). _reason_family already collapsed these six codes to one
# demand while _failure_layer left three of them in the contract catch-all and
# no rung set held them, so family-3480407b ended after one attempt with a
# mechanical scope trim still unspent.
SCOPE_FAMILY_REASONS = frozenset(
    {
        "relation_scope_mismatch",
        "scope_qualifier_missing",
        "scope_qualifier_not_source_bound",
        "scope_qualifier_not_displayed",
        "scope_value_not_source_supported",
        "answer_scope_not_source_bound",
        "answer_verifier_scope_not_source_bound",
        "reconstruction_scope_not_source_bound",
        # reconstruction-record-v2 sibling of the code above (gates slice,
        # chapter 2 yield audit 4.3 f).
        "reconstruction_scope_contradicts_answer",
    }
)
# Routing's own terminal outcomes. Each one records that a rewrite has nothing
# to act on, so no rung set may hold them and no repair may carry them.
UNROUTABLE_OUTCOME_REASONS = frozenset(
    {
        # every judge record present reports clean and no diagnostic names a
        # phrase, a detail type or a scope field: the gates contradict each other
        "gate_contradiction_unroutable",
        # a judge names a defect but the rejection carries no displayed words,
        # so the writer would be rewriting blind
        "empty_diagnostic_unroutable",
    }
)
# A weak judge may not buy a repair cycle (chapter 2 yield audit 4.6 f, R6). The
# roster's own rank decides: rank 4 is the Pro judge, rank 1 the flash-lite one
# that answered "no" to "24 species" against "24".
MINIMUM_AGREEMENT_TRIGGER_STRENGTH = 4
# One slot_lookup call per paper, behind the answer-leak filter.
MAX_SLOT_LOOKUPS_PER_PAPER = 1
# One paper family reached ``maximum_paper_cost_usd``. The family is recorded
# and skipped; the run continues with the next paper.
PAPER_COST_CAP_REASON_CODE = "paper_cost_cap_reached"
# One paper family raised an exception while its candidate was being built,
# routed, given options or persisted. The family is settled and skipped; the run
# continues with the next paper. Three such faults ended three whole chapter 3
# runs on 2026-09-16 (a per-paper cap refusal, a settlement another worker owned,
# and an option repair trigger the contract refused).
CANDIDATE_PROCESSING_FAULT_REASON_CODE = "candidate_processing_fault"
CANDIDATE_PROCESSING_FAULT_CONTRACT_VERSION = "candidate-processing-fault-v1"
# A ledger message the producer can meet outside the broker seam, on a direct
# read of the shared ledger. Every other whole-run refusal carries the marker
# ``broker_provider.broker_boundary`` puts on it, so a new refusal message needs
# no registration here.
RUN_ENDING_LEDGER_STOPS = (
    "the paid-call broker is halted",
    "the shared paid-call ledger has an integrity halt",
    "the ledger is halted",
)
# Candidate rows that record a generation call rather than a benchmark item.
# They keep the 26 dead chapter 2 calls visible without entering path
# reconstruction, acceptance, or the run counts (audit 4.6 e and 4.9 C8).
INCOMPLETE_CANDIDATE_STATUSES = frozenset(
    {"incomplete_infra", "generation_incomplete", "generation_settled"}
)
# A completion label's outcome class, read back into the run's own vocabulary.
# ``paper_completion.DISPOSITION_OUTCOME_CLASSES`` is the one owner of the pair,
# so a skipped paper counts exactly as the paper the producer walked.
COMPLETION_CLASS_DISPOSITIONS = {
    outcome_class: disposition
    for disposition, outcome_class in (
        paper_completion.DISPOSITION_OUTCOME_CLASSES.items()
    )
}
COMPLETION_CLASS_FINAL_STATES = {
    "eligibility_excluded": "rejected",
    "eligibility_unresolved": "unresolved",
    "generation_accepted": "accepted",
    "generation_rejected": "generation_rejected",
    "incomplete_non_mcq": "incomplete_non_mcq",
    "paper_cost_cap_reached": "paper_cost_cap_reached",
}
COMPLETION_CLASS_COUNTS = {
    "eligibility_excluded": "eligibility_rejected",
    "eligibility_unresolved": "eligibility_unresolved",
    "generation_accepted": "accepted_base_questions",
    "generation_rejected": "generation_rejected",
    "incomplete_non_mcq": "incomplete_non_mcq",
    "paper_cost_cap_reached": "paper_cost_cap_reached",
}
# The one SQL predicate that keeps call records out of a benchmark-item query.
BENCHMARK_CANDIDATE_PREDICATE = "status NOT IN ({})".format(
    ",".join(f"'{status}'" for status in sorted(INCOMPLETE_CANDIDATE_STATUSES))
)
SURGICAL_CORRECTION_REASONS = frozenset(
    {
        "question_context_missing",
        "question_context_unnecessary",
        "question_context_required",
        "question_context_referent_unresolved",
        "question_context_invalid",
        # ch2 yield audit 4.2 (F7 part 2): the namespaced deterministic codes
        # keep the surgical rung of their unprefixed forms.
        "standalone_det_question_context_missing",
        "standalone_det_question_context_referent_unresolved",
        "standalone_undefined_acronym",
        "standalone_undefined_unit_meaning",
        "standalone_undefined_percentage_basis",
        "standalone_undefined_period_or_event",
        "standalone_undefined_location",
        "standalone_undefined_population_or_sample",
    }
)
# One primary layer per repair, leakage first: a leaked item is worthless, a
# finding defect cannot be reworded, and evidence outranks wording because a
# wording fix on a disputed answer is waste. Contract codes come last because
# they are usually derived from the context defect above them.
_LAYER_PRIORITY = ("leakage", "finding", "evidence", "context", "options", "contract")
_SLOT_REASON_TYPES = {
    "standalone_undefined_location": "place",
    "standalone_undefined_period_or_event": "period",
    "standalone_undefined_population_or_sample": "sample",
    "standalone_undefined_acronym": "acronym",
    # Cost slice (audit 4.4): the writer-declared gaps demand the same slots.
    "writer_slot_unavailable_location": "place",
    "writer_slot_unavailable_period_or_event": "period",
    "writer_slot_unavailable_population_or_sample": "sample",
    "writer_slot_unavailable_acronym": "acronym",
}
_STANDALONE_DEPENDENT_REASONS = frozenset(
    {
        "relation_scope_mismatch",
        "answer_verifier_scope_not_source_bound",
        "reconstruction_scope_not_source_bound",
        # reconstruction-record-v2: the meaning test that replaced the
        # reconstructor wording test (chapter 2 yield audit 4.3 f).
        "reconstruction_scope_contradicts_answer",
        "answer_ambiguous",
        "question_claim_type_disagreement",
    }
)
_DEPENDENT_ROUTING_REASONS = {
    "question_context_referent_unresolved": frozenset(
        {"relation_scope_mismatch", "answer_verifier_scope_not_source_bound"}
    ),
    "question_context_missing": frozenset(
        {"relation_scope_mismatch", "answer_verifier_scope_not_source_bound"}
    ),
    # ch2 yield audit 4.2 (F7 part 2): the namespaced deterministic codes carry
    # the same downstream symptoms as their unprefixed forms.
    "standalone_det_question_context_referent_unresolved": frozenset(
        {"relation_scope_mismatch", "answer_verifier_scope_not_source_bound"}
    ),
    "standalone_det_question_context_missing": frozenset(
        {"relation_scope_mismatch", "answer_verifier_scope_not_source_bound"}
    ),
    "finding_answer_phrase_in_required_question_phrases": frozenset(
        {
            "question_answer_leakage",
            "question_context_answer_leakage",
            "scope_qualifier_missing",
        }
    ),
    # r15 audit RECON-2. The competing-alternatives check is the evidence for
    # the same defect that the reconstructor's own ambiguity label reports.
    # Both fire together by design, so the detail must not split the repair
    # across two failure layers and end the family.
    "answer_ambiguous": frozenset({"reconstruction_alternative_answer_present"}),
}


def run_stream(
    db: Database,
    namespace: Path,
    *,
    run_id: str,
    campaign_id: str,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    author: Provider,
    verifier: Provider,
    max_papers: int,
    progress_file: Path | None = None,
    eligibility_prompt_file: Path | None = None,
    eligibility_schema_file: Path | None = None,
    eligibility_policy_file: Path | None = None,
    eligibility_rescreen_prompt_file: Path | None = None,
    roles_file: Path | None = None,
    role_profile: str | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    if max_papers < 1:
        raise ValueError("max papers must be at least 1")
    model_roles = _resolve_model_roles(
        author=author,
        verifier=verifier,
        roles_file=roles_file,
        role_profile=role_profile,
    )
    access_manifest = _read(access_run_dir / "run-manifest.json")
    if _read(access_run_dir / "progress.json").get("state") != "completed":
        raise ValueError("the article-access run is not complete")
    if not (access_run_dir / "run-receipt.json").is_file():
        raise ValueError("the article-access completion receipt is missing")
    access_items = {
        item["candidate_key"]: item
        for item in (
            _read(path) for path in sorted((access_run_dir / "items").glob("*.json"))
        )
    }
    eligibility_jobs = _load_eligibility_jobs(
        eligibility_run_dir,
        prompt_file=eligibility_prompt_file,
        schema_file=eligibility_schema_file,
        policy_file=eligibility_policy_file,
        rescreen_prompt_file=eligibility_rescreen_prompt_file,
    )
    selection = access_manifest.get("selection")
    if not isinstance(selection, list):
        raise ValueError("the article-access selection is missing")
    if len(selection) != access_manifest.get("target_total"):
        raise ValueError("the ordered selection count is inconsistent")
    if max_papers > len(selection):
        raise ValueError("max papers cannot exceed the ordered selection count")
    for position, selected in enumerate(selection, start=1):
        access = access_items.get(selected.get("candidate_key"))
        if (
            access is None
            or selected.get("position") != position
            or access.get("position") != position
            or access.get("run_id") != access_manifest.get("run_id")
            or access.get("subgroup") != selected.get("subgroup")
        ):
            raise ValueError("the ordered selection does not match its access item")
    author_broker = getattr(author, "broker", None)
    verifier_broker = getattr(verifier, "broker", None)
    if author_broker is not None or verifier_broker is not None:
        if author_broker is None or author_broker is not verifier_broker:
            raise ValueError("streaming live providers must use one shared broker")
        if not all(
            (
                eligibility_prompt_file,
                eligibility_schema_file,
                eligibility_policy_file,
            )
        ):
            raise ValueError("streaming live eligibility inputs are required")
        if author_broker.stream_input_binding_required():
            author_broker.bind_stream_input(
                access_run_dir,
                phase=str(getattr(author, "phase", "")),
                run_id=run_id,
                campaign_id=campaign_id,
                eligibility_prompt_file=eligibility_prompt_file,
                eligibility_schema_file=eligibility_schema_file,
                eligibility_policy_file=eligibility_policy_file,
            )
    run_manifest_file = _write_run_manifest(
        namespace,
        run_id=run_id,
        campaign_id=campaign_id,
        access_run_dir=access_run_dir,
        eligibility_run_dir=eligibility_run_dir,
        access_manifest=access_manifest,
        author=author,
        verifier=verifier,
        eligibility_prompt_file=eligibility_prompt_file,
        eligibility_schema_file=eligibility_schema_file,
        eligibility_policy_file=eligibility_policy_file,
        eligibility_rescreen_prompt_file=eligibility_rescreen_prompt_file,
        model_roles=model_roles,
    )
    # The completion labels of this run. A labelled paper is finished, so it is
    # skipped before any of its receipts is read: the walk that re-validates
    # every receipt of every visited paper took about 22 minutes before the
    # first paid call at about 5,000 receipts. The label is a note about work
    # already finished; no receipt is altered or deleted.
    completions = paper_completion.load_completions(db, run_id=run_id)
    completion_commit = code_commit or "unknown"
    trusted_eligibility_decisions: dict[str, str] = {}
    if verifier_broker is not None:
        trusted_eligibility_decisions = _trusted_brokered_eligibility_decisions(
            selection=selection,
            access_items=access_items,
            eligibility_jobs=eligibility_jobs,
            verifier=verifier,
            max_papers=max_papers,
            prompt_file=eligibility_prompt_file,
            schema_file=eligibility_schema_file,
            policy_file=eligibility_policy_file,
            rescreen_prompt_file=eligibility_rescreen_prompt_file,
            completions=completions,
        )
    progress = _Progress(
        progress_file or namespace / "streaming-dataset-r1" / "progress.json",
        run_id=campaign_id,
        invocation_run_id=run_id,
        run_manifest_file=run_manifest_file,
        counts={
            "full_text_ready": sum(
                item.get("access_state") == "full_text_ready"
                for item in access_items.values()
            ),
            "eligibility_completed": len(trusted_eligibility_decisions)
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision")
                in {"eligible", "excluded", "uncertain"}
                for item in eligibility_jobs.values()
            ),
            "eligible": sum(
                value == "eligible" for value in trusted_eligibility_decisions.values()
            )
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision") == "eligible"
                for item in eligibility_jobs.values()
            ),
            "excluded": sum(
                value == "excluded" for value in trusted_eligibility_decisions.values()
            )
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision") == "excluded"
                for item in eligibility_jobs.values()
            ),
            "unresolved": sum(
                value == "uncertain" for value in trusted_eligibility_decisions.values()
            )
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision") == "uncertain"
                for item in eligibility_jobs.values()
            ),
            "generation_rejected": 0,
            "paper_cost_cap_reached": 0,
            "accepted_qa": _accepted_count(db, campaign_id),
        },
    )
    if author_broker is not None or verifier_broker is not None:
        progress.attach_broker(author_broker)
    progress.write("running", "eligibility", "Streaming pipeline started.")
    counts = {
        "accepted_base_questions": 0,
        CANDIDATE_PROCESSING_FAULT_REASON_CODE: 0,
        "completion_labelled_skipped": 0,
        "eligibility_rejected": 0,
        "eligibility_unresolved": 0,
        "generation_rejected": 0,
        "incomplete_non_mcq": 0,
        "paper_cost_cap_reached": 0,
        "processed": 0,
    }
    paper_results: list[dict[str, Any]] = []
    resumed_papers = 0
    for selected in selection:
        if counts["processed"] >= max_papers:
            break
        candidate_key = selected.get("candidate_key")
        access = access_items.get(candidate_key)
        eligibility = eligibility_jobs.get(candidate_key)
        if access is None or access.get("access_state") != "full_text_ready":
            continue
        paper_id = str(candidate_key)
        family_id = str(
            access.get("paper_family_id")
            or stable_id("family", access.get("doi") or candidate_key)
        )
        completion = completions.get(paper_id)
        if completion is not None:
            # A labelled paper is finished. It is counted and skipped here, so
            # none of its receipts is read and no call of it is replayed.
            outcome_class = str(completion["outcome_class"])
            counts[COMPLETION_CLASS_COUNTS[outcome_class]] += 1
            counts["processed"] += 1
            counts["completion_labelled_skipped"] += 1
            if outcome_class in {"generation_rejected", "paper_cost_cap_reached"}:
                progress.increment(outcome_class)
            # The progress record lists the paper as a replay would, so the
            # viewer reads the same run whether a paper was walked or skipped.
            progress.paper(
                paper_id=str(completion["source_id"] or candidate_key),
                title=access.get("title"),
                current_stage="completed",
                final_state=COMPLETION_CLASS_FINAL_STATES[outcome_class],
                final_reason=(
                    "machine_accepted_unverified"
                    if outcome_class == "generation_accepted"
                    else str(
                        completion["reason_code"]
                        or COMPLETION_CLASS_DISPOSITIONS[outcome_class]
                    )
                ),
            )
            paper_results.append(
                {
                    "candidate_key": candidate_key,
                    "disposition": COMPLETION_CLASS_DISPOSITIONS[outcome_class],
                    "reason_codes": (
                        [str(completion["reason_code"])]
                        if completion["reason_code"]
                        else []
                    ),
                    "source_id": completion["source_id"],
                    "completion_label": {
                        "outcome_class": outcome_class,
                        "labelled_at_utc": completion["labelled_at_utc"],
                        "labelled_by_commit": completion["labelled_by_commit"],
                    },
                }
            )
            continue
        # Containment (chapter 3 candidate fault slice): one paper family is one
        # unit of work. An exception its candidate, routing, option or persistence
        # code raises settles the family and the producer moves to the next paper.
        # Three faults of one family ended three whole runs on 2026-09-16.
        source_id: str | None = None
        progress.last_error_stage = None
        processed_before = counts["processed"]
        results_before = len(paper_results)
        try:
            source_version_id = str(access["source_content_hash"])
            operational_unresolved = _operational_unresolved_families(verifier_broker)
            if family_id in operational_unresolved:
                unresolved = operational_unresolved[family_id]
                request_key = unresolved["request_key"]
                reason_code = unresolved["reason_code"]
                _record_operational_unresolved(
                    db,
                    campaign_id=campaign_id,
                    candidate_key=str(candidate_key),
                    selected=selected,
                    family_id=family_id,
                    request_key=request_key,
                    reason_code=reason_code,
                )
                # audit 4.9 C8: the family keeps its in-flight call records, so the
                # completed calls of an ambiguous charge stay visible for diagnosis
                # instead of vanishing with the attempt.
                counts.setdefault("incomplete_infra", 0)
                counts["incomplete_infra"] += len(
                    _incomplete_infra_records(db, campaign_id, family_id)
                )
                counts.setdefault("operational_unresolved", 0)
                counts["operational_unresolved"] += 1
                counts["processed"] += 1
                progress.increment("unresolved")
                paper_results.append(
                    {
                        "candidate_key": candidate_key,
                        "disposition": "operational_unresolved",
                        "reason_codes": [reason_code],
                        "source_id": None,
                        "broker_request_key": request_key,
                    }
                )
                progress.paper(
                    paper_id=candidate_key,
                    title=access.get("title"),
                    current_stage="completed",
                    final_state="unresolved",
                    final_reason=reason_code,
                )
                continue
            paper_author = _bind_provider(
                author,
                paper_id=paper_id,
                family_id=family_id,
                source_version_id=source_version_id,
            )
            paper_verifier = _bind_provider(
                verifier,
                paper_id=paper_id,
                family_id=family_id,
                source_version_id=source_version_id,
            )
            if verifier_broker is not None and (
                eligibility is None
                or eligibility.get("execution_authority") != "shared_gemini_broker"
            ):
                eligibility = None
            if eligibility is None:
                if not all(
                    (
                        eligibility_prompt_file,
                        eligibility_schema_file,
                        eligibility_policy_file,
                    )
                ):
                    raise ValueError(
                        "streaming eligibility inputs are required for a newly ready paper"
                    )
                try:
                    eligibility = _run_eligibility(
                        db,
                        access,
                        eligibility_run_dir,
                        run_id=campaign_id,
                        provider=paper_verifier,
                        prompt_file=eligibility_prompt_file,
                        schema_file=eligibility_schema_file,
                        policy_file=eligibility_policy_file,
                        rescreen_prompt_file=eligibility_rescreen_prompt_file,
                    )
                except PaperCostCapError as error:
                    # The family reached the per-paper cost cap before it was
                    # screened. Nothing is charged past the cap, so the family is
                    # recorded and the run continues with the next paper.
                    _record_paper_cost_cap(
                        db,
                        campaign_id=campaign_id,
                        candidate_key=str(candidate_key),
                        source_id=None,
                        family_id=family_id,
                        selected=selected,
                        error=error,
                        cost_state=_family_cost_state(paper_verifier, family_id),
                    )
                    counts["paper_cost_cap_reached"] += 1
                    counts["processed"] += 1
                    progress.increment("paper_cost_cap_reached")
                    paper_results.append(
                        {
                            "candidate_key": candidate_key,
                            "disposition": "paper_cost_cap_reached",
                            "reason_codes": [PAPER_COST_CAP_REASON_CODE],
                            "source_id": None,
                        }
                    )
                    progress.paper(
                        paper_id=candidate_key,
                        title=access.get("title"),
                        current_stage="completed",
                        final_state="paper_cost_cap_reached",
                        final_reason=PAPER_COST_CAP_REASON_CODE,
                    )
                    _label_completed_paper(
                        db,
                        completions,
                        run_id=run_id,
                        campaign_id=campaign_id,
                        candidate_key=paper_id,
                        family_id=family_id,
                        source_id=None,
                        disposition="paper_cost_cap_reached",
                        reason_codes=[PAPER_COST_CAP_REASON_CODE],
                        eligibility_decision=None,
                        code_commit=completion_commit,
                    )
                    continue
                except Exception as error:
                    progress.error(
                        candidate_key, access.get("title"), "eligibility", error
                    )
                    raise
                eligibility_jobs[candidate_key] = eligibility
                if verifier_broker is None:
                    progress.set_count(
                        "eligible",
                        sum(
                            (item.get("validation") or {}).get("decision") == "eligible"
                            for item in eligibility_jobs.values()
                        ),
                    )
                    progress.set_count(
                        "eligibility_completed",
                        sum(
                            (item.get("validation") or {}).get("decision")
                            in {"eligible", "excluded", "uncertain"}
                            for item in eligibility_jobs.values()
                        ),
                    )
                    progress.set_count(
                        "excluded",
                        sum(
                            (item.get("validation") or {}).get("decision") == "excluded"
                            for item in eligibility_jobs.values()
                        ),
                    )
                    progress.set_count(
                        "unresolved",
                        sum(
                            (item.get("validation") or {}).get("decision")
                            == "uncertain"
                            for item in eligibility_jobs.values()
                        ),
                    )
            progress.write("running", "eligibility", f"Checking {candidate_key}.")
            try:
                deterministic_unresolved = False
                if verifier_broker is not None:
                    _validate_access_integrity(access, eligibility)
                    validation = _validate_brokered_eligibility(
                        eligibility,
                        paper_verifier,
                        access=access,
                        prompt_file=eligibility_prompt_file,
                        schema_file=eligibility_schema_file,
                        policy_file=eligibility_policy_file,
                        rescreen_prompt_file=eligibility_rescreen_prompt_file,
                    )
                    eligibility = {
                        **eligibility,
                        "state": _brokered_eligibility_state(eligibility, validation),
                        "validation": validation,
                    }
                    deterministic_unresolved = validation["valid"] is not True
                if deterministic_unresolved:
                    if validation.get("decision") != "uncertain":
                        raise ValueError(
                            "invalid deterministic eligibility must remain uncertain"
                        )
                else:
                    _validate_pair(access, eligibility)
            except Exception as error:
                progress.error(candidate_key, access.get("title"), "eligibility", error)
                raise
            decision = eligibility["validation"]["decision"]
            if verifier_broker is not None:
                trusted_eligibility_decisions[candidate_key] = decision
                progress.set_count(
                    "eligibility_completed", len(trusted_eligibility_decisions)
                )
                progress.set_count(
                    "eligible",
                    sum(
                        value == "eligible"
                        for value in trusted_eligibility_decisions.values()
                    ),
                )
                progress.set_count(
                    "excluded",
                    sum(
                        value == "excluded"
                        for value in trusted_eligibility_decisions.values()
                    ),
                )
                progress.set_count(
                    "unresolved",
                    sum(
                        value == "uncertain"
                        for value in trusted_eligibility_decisions.values()
                    ),
                )
            if decision != "eligible":
                validation = eligibility["validation"]
                unresolved = validation["valid"] is not True or decision == "uncertain"
                reason_codes = (
                    validation.get("errors") or ["eligibility_validation_unresolved"]
                    if validation["valid"] is not True
                    else validation.get(
                        "overall_reason_codes",
                        eligibility["parsed_response"].get(
                            "overall_reason_codes", ["eligibility_unresolved"]
                        ),
                    )
                )
                disposition = (
                    "eligibility_unresolved" if unresolved else "eligibility_rejected"
                )
                for reason_code in reason_codes:
                    with db.transaction():
                        db.connection.execute(
                            """INSERT OR IGNORE INTO rejection_ledger
                            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
                            VALUES (?,NULL,NULL,'scientific_eligibility',?,?,?)""",
                            (
                                stable_id(
                                    "rejection",
                                    campaign_id,
                                    candidate_key,
                                    "scientific_eligibility",
                                    reason_code,
                                ),
                                reason_code,
                                canonical_json(
                                    {
                                        "candidate_key": candidate_key,
                                        "eligibility_job_key": eligibility["job_key"],
                                        "decision": decision,
                                        "validation_valid": validation["valid"],
                                        "validation_errors": validation.get(
                                            "errors", []
                                        ),
                                        "selection": selected,
                                    }
                                ),
                                now(),
                            ),
                        )
                paper_results.append(
                    {
                        "candidate_key": candidate_key,
                        "disposition": disposition,
                        "reason_codes": reason_codes,
                        "source_id": None,
                    }
                )
                counts[disposition] += 1
                counts["processed"] += 1
                progress.paper(
                    paper_id=candidate_key,
                    title=access.get("title"),
                    current_stage="completed",
                    final_state="unresolved" if unresolved else "rejected",
                    final_reason=reason_codes[0],
                )
                _label_completed_paper(
                    db,
                    completions,
                    run_id=run_id,
                    campaign_id=campaign_id,
                    candidate_key=paper_id,
                    family_id=family_id,
                    source_id=None,
                    disposition=disposition,
                    reason_codes=list(reason_codes),
                    eligibility_decision=decision,
                    code_commit=completion_commit,
                )
                continue
            try:
                source_id = _import_source(
                    db,
                    namespace,
                    access,
                    selected,
                    eligibility,
                    family_id=family_id,
                )
            except Exception as error:
                progress.error(
                    candidate_key, access.get("title"), "source_import", error
                )
                raise
            generation_result = _progress_generation(
                db,
                namespace,
                progress,
                campaign_id=campaign_id,
                candidate_key=str(candidate_key),
                source_id=source_id,
                family_id=family_id,
                selected=selected,
                source_version_id=source_version_id,
                title=access.get("title"),
                author=paper_author,
                verifier=paper_verifier,
            )
            if generation_result["resumed"]:
                resumed_papers += 1
            disposition = generation_result["disposition"]
            reason_codes = generation_result["reason_codes"]
            counts[
                {
                    "generation_rejected": "generation_rejected",
                    "accepted": "accepted_base_questions",
                    "incomplete_non_mcq": "incomplete_non_mcq",
                    "paper_cost_cap_reached": "paper_cost_cap_reached",
                }[disposition]
            ] += 1
            counts["processed"] += 1
            if disposition == "generation_rejected":
                progress.increment("generation_rejected")
            elif disposition == "paper_cost_cap_reached":
                progress.increment("paper_cost_cap_reached")
            elif disposition == "accepted":
                progress.set_count("accepted_qa", _accepted_count(db, campaign_id))
            paper_results.append(
                {
                    "candidate_key": candidate_key,
                    "disposition": disposition,
                    "reason_codes": reason_codes,
                    "source_id": source_id,
                }
            )
            progress.paper(
                paper_id=source_id,
                title=access.get("title"),
                current_stage="completed",
                final_state=disposition,
                final_reason=(
                    "machine_accepted_unverified"
                    if disposition == "accepted"
                    else (reason_codes or [disposition])[0]
                ),
            )
            _label_completed_paper(
                db,
                completions,
                run_id=run_id,
                campaign_id=campaign_id,
                candidate_key=paper_id,
                family_id=family_id,
                source_id=source_id,
                disposition=disposition,
                reason_codes=list(reason_codes or []),
                eligibility_decision=decision,
                code_commit=completion_commit,
            )
        except Exception as error:
            if _ends_the_run(error):
                raise
            fault = _contain_candidate_processing_fault(
                db,
                progress,
                campaign_id=campaign_id,
                candidate_key=str(candidate_key),
                source_id=source_id,
                family_id=family_id,
                selected=selected,
                title=access.get("title"),
                provider=verifier,
                error=error,
            )
            counts[CANDIDATE_PROCESSING_FAULT_REASON_CODE] += 1
            progress.increment(CANDIDATE_PROCESSING_FAULT_REASON_CODE)
            # The paper is counted one time. A fault after its own disposition
            # was already recorded keeps that record and adds no second row.
            if counts["processed"] == processed_before:
                counts["processed"] += 1
            if len(paper_results) == results_before:
                paper_results.append(
                    {
                        "candidate_key": candidate_key,
                        "disposition": CANDIDATE_PROCESSING_FAULT_REASON_CODE,
                        "reason_codes": [CANDIDATE_PROCESSING_FAULT_REASON_CODE],
                        "source_id": source_id,
                        "candidate_processing_fault": fault,
                    }
                )
            continue
    progress.write("running", "export", "Writing validated dataset exports.")
    try:
        exported = export_run(
            db,
            namespace,
            campaign_id,
            seed="streaming-20260912",
            candidate_schema_version=generation_contract.CANDIDATE_SCHEMA_VERSION,
            generation_prompt_version=generation_contract.PROMPT_VERSION,
        )
    except Exception as error:
        progress.write(
            "error", "export", f"Streaming stopped on {type(error).__name__}."
        )
        raise
    progress.set_dataset_metadata(
        namespace / "exports" / exported["export_id"] / "manifest.json"
    )
    # One broker provider can serve both the author and the verifier while
    # metering a different model for each role, so the disclosure reads the
    # per-role effective models, never the two provider objects.
    same_model_roles = bool(model_roles["same_model_roles"])
    if model_roles["enforced"] and same_model_roles:
        raise ValueError("an enforced model role run kept one model in every role")
    live_provider = bool(getattr(author, "externally_metered", False))
    generation_counts = _generation_counts(db, campaign_id)
    result = {
        "state": "completed",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "counts": counts,
        "paper_results": paper_results,
        "resumed_papers": resumed_papers,
        "generation_counts": generation_counts,
        "export": exported,
        "provider_policy": {
            "model": author.model,
            "same_model_roles": same_model_roles,
            "correlated_error_disclosed": same_model_roles,
            "live_provider": live_provider,
            "model_roles": model_roles,
        },
    }
    progress.write("completed", "completed", "Streaming pipeline completed.")
    return result


def _label_completed_paper(
    db: Database,
    completions: dict[str, dict[str, Any]],
    *,
    run_id: str,
    campaign_id: str,
    candidate_key: str,
    family_id: str,
    source_id: str | None,
    disposition: str,
    reason_codes: list[str],
    eligibility_decision: str | None,
    code_commit: str,
) -> None:
    """Label one paper the run has just finished, and hold it in this session.

    The label is written the moment the paper reaches a terminal outcome, so the
    batch catch-up is needed one time only. A disposition that left the paper
    mid-family takes no label and keeps today's behaviour.
    """
    row = paper_completion.label_from_disposition(
        run_id=run_id,
        campaign_id=campaign_id,
        candidate_key=candidate_key,
        family_id=family_id,
        source_id=source_id,
        disposition=disposition,
        reason_codes=reason_codes,
        eligibility_decision=eligibility_decision,
        code_commit=code_commit,
    )
    if row is None:
        return
    paper_completion.record_completion(db, row)
    completions[candidate_key] = row


def _progress_generation(
    db: Database,
    namespace: Path,
    progress: _Progress,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    family_id: str,
    selected: dict[str, Any],
    title: str | None,
    author: Provider,
    verifier: Provider,
    source_version_id: str = "",
    pending_handler: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    try:
        paths = _generation_paths(
            db,
            campaign_id=campaign_id,
            source_id=source_id,
            family_id=family_id,
        )
    except Exception as error:
        progress.error(source_id, title, "generation", error)
        raise
    resumed = bool(paths)
    generation_attempt_supported = _supports_generation_attempt()
    slot_lookups_used = 0

    def slot_lookup(
        *,
        failed_path: dict[str, Any],
        evidence: dict[str, Any],
        slot: str,
    ) -> dict[str, Any] | None:
        """Run the one slot lookup this paper is allowed, or none."""
        nonlocal slot_lookups_used
        if slot_lookups_used >= MAX_SLOT_LOOKUPS_PER_PAPER:
            return None
        slot_lookups_used += 1
        return _run_slot_lookup(
            db,
            author,
            run_id=campaign_id,
            failed_path=failed_path,
            evidence=evidence,
            source_id=source_id,
            slot=slot,
        )

    while True:
        for path in sorted(paths.values(), key=_path_sort_key):
            candidate_row = path.get("candidate")
            if candidate_row is None:
                continue
            candidate = json.loads(candidate_row["candidate_json"])
            if candidate_row["status"] not in {
                "candidate",
                "qa_gate_failed",
            }:
                continue
            try:
                validation = validate_candidate(db, namespace, candidate).as_dict()
            except Exception as error:
                progress.error(source_id, title, "validation", error)
                raise
            path["validation"] = validation
            path["candidate"] = _candidate_row(db, candidate["item_id"])

        outcome = _stored_generation_outcome(
            db,
            paths,
            campaign_id=campaign_id,
            family_id=family_id,
            source_id=source_id,
            slot_lookup=slot_lookup,
        )
        if outcome["kind"] == STORED_OUTCOME_UNVALIDATED:
            raise ValueError(
                "a generated candidate remained unvalidated after its validation checkpoint"
            )
        if outcome["kind"] == STORED_OUTCOME_SLOT_LOOKUP_REQUIRED:
            raise ValueError("the producer refused its own slot lookup")
        if outcome["kind"] == STORED_OUTCOME_TERMINAL:
            gate_review = outcome.get("gate_review")
            if gate_review is not None:
                _record_gate_review_flag(
                    db,
                    campaign_id=campaign_id,
                    candidate_key=candidate_key,
                    source_id=source_id,
                    selected=selected,
                    attempt=gate_review["attempt"],
                    reason_code=gate_review["reason_code"],
                    rejection_reason_codes=gate_review["rejection_reason_codes"],
                )
            return {
                "disposition": outcome["disposition"],
                "reason_codes": outcome["reason_codes"],
                "resumed": resumed,
            }
        next_attempt = outcome["attempt"]
        progress.paper(
            paper_id=source_id,
            title=title,
            current_stage="generation",
        )
        # audit 4.9 C8 and audit 4.6 e: the call is on the record before the
        # provider can charge for it, so an ambiguous charge, a crash or a dead
        # call leaves a row instead of nothing.
        call_record_id = _open_generation_call_record(
            db,
            run_id=campaign_id,
            source_id=source_id,
            family_id=family_id,
            source_version_id=source_version_id,
            attempt=next_attempt,
        )
        try:
            candidate = _generate_candidate_attempt(
                db,
                namespace,
                source_id=source_id,
                run_id=campaign_id,
                attempt=next_attempt,
                author=author,
                verifier=verifier,
            )
        except (CandidateRejectedError, ProviderResponseError) as error:
            reason_code = error.reason_code
            _settle_generation_call_record(
                db,
                call_record_id,
                state="generation_incomplete",
                reason_code=reason_code,
            )
            _record_generation_rejection(
                db,
                campaign_id=campaign_id,
                candidate_key=candidate_key,
                source_id=source_id,
                selected=selected,
                attempt=next_attempt,
                reason_code=reason_code,
                error=error,
            )
            paths[_path_key(next_attempt)] = {
                "attempt": next_attempt,
                "candidate": None,
                "candidate_status": None,
                "rejection": {"reason_codes": [reason_code]},
                "legacy": not generation_attempt_supported,
            }
            continue
        except PaperCostCapError as error:
            # The per-paper cost cap bounds one family, never the run. Nothing is
            # charged past the cap; the family is recorded and the producer moves
            # to the next paper. Only a whole-run stop ends the producer.
            _settle_generation_call_record(
                db,
                call_record_id,
                state="generation_incomplete",
                reason_code=PAPER_COST_CAP_REASON_CODE,
            )
            _record_paper_cost_cap(
                db,
                campaign_id=campaign_id,
                candidate_key=candidate_key,
                source_id=source_id,
                family_id=family_id,
                selected=selected,
                error=error,
                cost_state=_family_cost_state(author, family_id),
                attempt=next_attempt,
            )
            return {
                "disposition": "paper_cost_cap_reached",
                "reason_codes": [PAPER_COST_CAP_REASON_CODE],
                "resumed": resumed,
            }
        except BudgetError as error:
            if str(error) != PER_REQUEST_CAP_REASON:
                progress.error(source_id, title, "generation", error)
                raise
            _settle_generation_call_record(
                db,
                call_record_id,
                state="generation_incomplete",
                reason_code="request_cost_bound_exceeded",
            )
            _record_budget_stop(
                db,
                campaign_id=campaign_id,
                candidate_key=candidate_key,
                source_id=source_id,
                selected=selected,
                attempt=next_attempt,
                error=error,
            )
            return {
                "disposition": "generation_rejected",
                "reason_codes": ["request_cost_bound_exceeded"],
                "resumed": resumed,
            }
        except ValueError as error:
            if (
                next_attempt["attempt_kind"] == "alternative_finding"
                and str(error) == "alternative finding state is invalid"
            ):
                reason_code = "alternative_finding_state_invalid"
                _settle_generation_call_record(
                    db,
                    call_record_id,
                    state="generation_incomplete",
                    reason_code=reason_code,
                )
                _record_generation_rejection(
                    db,
                    campaign_id=campaign_id,
                    candidate_key=candidate_key,
                    source_id=source_id,
                    selected=selected,
                    attempt=next_attempt,
                    reason_code=reason_code,
                    error=error,
                )
                paths[_path_key(next_attempt)] = {
                    "attempt": next_attempt,
                    "candidate": None,
                    "candidate_status": None,
                    "rejection": {"reason_codes": [reason_code]},
                    "partial_finding": False,
                }
                continue
            progress.error(source_id, title, "generation", error)
            raise
        except Exception as error:
            if pending_handler is not None and getattr(error, "code", None) == (
                "BATCH_PENDING"
            ):
                pending_handler(next_attempt)
            progress.error(source_id, title, "generation", error)
            raise

        try:
            validation = validate_candidate(db, namespace, candidate).as_dict()
        except Exception as error:
            progress.error(source_id, title, "validation", error)
            raise
        candidate_row = _candidate_row(db, candidate["item_id"])
        if candidate_row is None:
            raise ValueError("generation returned a candidate that was not persisted")
        _settle_generation_call_record(
            db,
            call_record_id,
            state="generation_settled",
            candidate_item_id=candidate["item_id"],
        )
        candidate_provenance = candidate.get("provenance") or {}
        if (
            generation_attempt_supported
            and "generation_attempt" not in candidate_provenance
        ):
            raise ValueError("generation did not persist its attempt provenance")
        candidate_attempt = _candidate_generation_attempt(candidate, next_attempt)
        if candidate_attempt != next_attempt:
            raise ValueError("generation returned a candidate for another attempt")
        path = {
            "attempt": candidate_attempt,
            "candidate": candidate_row,
            "candidate_status": candidate_row["status"],
            "validation": validation,
            "legacy": not generation_attempt_supported,
            "partial_finding": False,
        }
        paths[_path_key(path["attempt"])] = path
        if (
            validation["final_label"] == "machine_accepted_unverified"
            and validation["labels"]["mcq_eligible"]
        ):
            if hasattr(author, "record_accepted"):
                author.record_accepted(
                    family_id=family_id, item_id=candidate["item_id"]
                )
            return {
                "disposition": "accepted",
                "reason_codes": validation["reasons"],
                "resumed": resumed,
            }
        if validation["final_label"] == "machine_accepted_unverified":
            with db.transaction():
                db.connection.execute(
                    "UPDATE candidates SET status='incomplete_non_mcq',updated_at=? WHERE item_id=?",
                    (now(), candidate["item_id"]),
                )
            path["candidate"] = _candidate_row(db, candidate["item_id"])
            path["candidate_status"] = "incomplete_non_mcq"
            if not generation_attempt_supported:
                return {
                    "disposition": "incomplete_non_mcq",
                    "reason_codes": validation["reasons"],
                    "resumed": resumed,
                }


# The three answers the stored generation paths of one paper family can give.
# ``_stored_generation_outcome`` is the one owner of that ladder: the producer
# walks it to find its next paid call, and the per-paper completion label walks
# the same ladder to decide whether the run already finished the paper. Two
# owners would let a labelled paper differ from the paper the producer would
# have finished.
STORED_OUTCOME_TERMINAL = "terminal"
STORED_OUTCOME_ATTEMPT = "attempt"
STORED_OUTCOME_UNVALIDATED = "unvalidated"
STORED_OUTCOME_SLOT_LOOKUP_REQUIRED = "slot_lookup_required"


def _stored_generation_outcome(
    db: Database,
    paths: dict[tuple[int, int], dict[str, Any]],
    *,
    campaign_id: str,
    family_id: str,
    source_id: str,
    slot_lookup: Callable[..., dict[str, Any] | None] | None = None,
    require_validation_events: bool = True,
) -> dict[str, Any]:
    """Read one paper family's next step from its stored generation paths.

    The answer is one of four kinds. ``terminal`` names the disposition the run
    reached with no further call. ``attempt`` carries the next generation
    attempt, which costs money. ``unvalidated`` says a stored candidate still
    needs its local validation, which the caller runs before it asks again.
    ``slot_lookup_required`` says routing wants a slot lookup call and this
    caller passed no ``slot_lookup``.

    Each stored candidate carries its own validation, read back from the
    validation event of that exact payload. A caller that has already validated
    a path leaves that path's ``validation`` in place. A producer treats a
    missing event as a fault; a read-only caller passes
    ``require_validation_events`` false and takes ``unvalidated`` instead.

    Nothing here writes to the database. A terminal answer that also reached a
    gate review flag reports it in ``gate_review`` and the caller records it, so
    a read-only caller stays read-only.
    """
    for path in paths.values():
        candidate_row = path.get("candidate")
        if candidate_row is None or path.get("validation") is not None:
            continue
        event = _validation_event_for_stored_candidate(db, candidate_row)
        if event is None:
            if require_validation_events:
                raise ValueError(
                    "a terminal streaming candidate lacks a validation event "
                    "for its payload"
                )
            return {"kind": STORED_OUTCOME_UNVALIDATED}
        path["validation"] = _validation_result_from_event(db, candidate_row, event)

    accepted = _accepted_generation_path(paths)
    if accepted is not None:
        event = _require_validation_event(db, accepted["candidate"])
        return {
            "kind": STORED_OUTCOME_TERMINAL,
            "disposition": "accepted",
            "reason_codes": _reason_codes(event),
        }

    budget_stop = next(
        (
            path
            for path in sorted(paths.values(), key=_path_sort_key)
            if path.get("budget_stop")
        ),
        None,
    )
    if budget_stop is not None:
        return {
            "kind": STORED_OUTCOME_TERMINAL,
            "disposition": "generation_rejected",
            "reason_codes": _path_failure(db, budget_stop)["reason_codes"],
        }

    partial = next(
        (
            path
            for path in sorted(paths.values(), key=_path_sort_key)
            if path.get("candidate") is not None
            and path["candidate"]["status"] in {"candidate", "qa_gate_failed"}
        ),
        None,
    )
    if partial is not None:
        return {"kind": STORED_OUTCOME_UNVALIDATED}

    partial_finding = next(
        (
            path
            for path in sorted(paths.values(), key=_path_sort_key)
            if path.get("partial_finding")
        ),
        None,
    )
    if partial_finding is not None:
        return {"kind": STORED_OUTCOME_ATTEMPT, "attempt": partial_finding["attempt"]}

    if not paths:
        return {
            "kind": STORED_OUTCOME_ATTEMPT,
            "attempt": _generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                finding_attempt_index=1,
                question_revision_index=0,
                attempt_kind="primary",
                parent_attempt_id=None,
                parent_item_id=None,
                trigger_reason_code=None,
                excluded_finding_span_ids=[],
            ),
        }

    failed_path = max(paths.values(), key=_path_sort_key)
    failure = _path_failure(db, failed_path)
    if not _supports_generation_attempt() and failed_path.get("legacy"):
        return {
            "kind": STORED_OUTCOME_TERMINAL,
            "disposition": (
                "incomplete_non_mcq"
                if failed_path.get("candidate_status") == "incomplete_non_mcq"
                else "generation_rejected"
            ),
            "reason_codes": failure["reason_codes"],
        }
    evidence = _routing_evidence(failed_path)
    slot_evidence = _slot_evidence_pool(db, source_id, evidence)
    # The satisfiability guard (judge-options slice) reads only the text the
    # writer saw, plus a found slot_lookup sentence below.
    forwarded_slot_evidence = _forwarded_slot_evidence(failed_path)
    unmet = _unmet_slot_demands(failure["reason_codes"], slot_evidence)
    if unmet:
        if slot_lookup is None:
            return {
                "kind": STORED_OUTCOME_SLOT_LOOKUP_REQUIRED,
                "unmet": sorted(unmet),
            }
        found = slot_lookup(
            failed_path=failed_path,
            evidence=evidence,
            slot=sorted(unmet)[0],
        )
        if found is not None:
            evidence["slot_lookup"] = found
            slot_evidence = frozenset({*(slot_evidence or frozenset()), found["slot"]})
            forwarded_slot_evidence = frozenset(
                {*(forwarded_slot_evidence or frozenset()), found["slot"]}
            )
    unroutable: list[str] = []
    next_attempt = _next_generation_attempt(
        campaign_id=campaign_id,
        family_id=family_id,
        paths=paths,
        failed_path=failed_path,
        reason_codes=failure["reason_codes"],
        slot_evidence=slot_evidence,
        forwarded_slot_evidence=forwarded_slot_evidence,
        evidence=evidence,
        unroutable=unroutable,
    )
    if next_attempt is not None:
        return {"kind": STORED_OUTCOME_ATTEMPT, "attempt": next_attempt}
    gate_review = (
        {
            "attempt": failed_path["attempt"],
            "reason_code": unroutable[0],
            "rejection_reason_codes": failure["reason_codes"],
        }
        if unroutable
        else None
    )
    incomplete = next(
        (
            path
            for path in sorted(paths.values(), key=_path_sort_key)
            if path.get("candidate_status") == "incomplete_non_mcq"
        ),
        None,
    )
    if incomplete is not None:
        # An accepted question whose distractors failed is a product output, so
        # it keeps its own disposition even when routing stopped the last path
        # for gate review.
        return {
            "kind": STORED_OUTCOME_TERMINAL,
            "disposition": "incomplete_non_mcq",
            "reason_codes": _path_failure(db, incomplete)["reason_codes"],
            "gate_review": gate_review,
        }
    return {
        "kind": STORED_OUTCOME_TERMINAL,
        "disposition": "generation_rejected",
        "reason_codes": [*failure["reason_codes"], *unroutable],
        "gate_review": gate_review,
    }


def _supports_generation_attempt() -> bool:
    try:
        parameters = inspect.signature(generate_candidate).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "generation_attempt"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _generate_candidate_attempt(
    db: Database,
    namespace: Path,
    *,
    source_id: str,
    run_id: str,
    attempt: dict[str, Any],
    author: Provider,
    verifier: Provider,
) -> dict[str, Any]:
    # One attempt drives several roles, each with its own stage timeout. The
    # broker applies the exact per-stage timeout itself, so this caller value
    # only has to be no shorter than the longest stage the attempt can reach.
    broker = getattr(author, "broker", None)
    timeout = (
        maximum_call_timeout_seconds(broker.config)
        if broker is not None
        else DEFAULT_CALL_TIMEOUT_SECONDS
    )
    arguments = {
        "source_id": source_id,
        "run_id": run_id,
        "arm": "answer_first",
        "author": author,
        "verifier": verifier,
        "budget_mode": "tokens",
        "budget_limit": Decimal("1000000"),
        "reservation": Decimal("100"),
        "timeout": timeout,
        "retries": 0,
        "rate_limit_seconds": 0,
    }
    if _supports_generation_attempt():
        arguments["generation_attempt"] = attempt
    return generate_candidate(db, namespace, **arguments)


def _candidate_row(db: Database, item_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT item_id,status,candidate_json FROM candidates WHERE item_id=?",
        (item_id,),
    )


def _generation_attempt(
    *,
    campaign_id: str,
    family_id: str,
    finding_attempt_index: int,
    question_revision_index: int,
    attempt_kind: str,
    parent_attempt_id: str | None,
    parent_item_id: str | None,
    trigger_reason_code: str | None,
    excluded_finding_span_ids: list[str],
    repair_numeric_rule: bool = False,
    slot_lookup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempt_id = stable_id(
        "generation-attempt",
        campaign_id,
        family_id,
        finding_attempt_index,
        question_revision_index,
        GENERATION_ATTEMPT_CONTRACT_VERSION,
    )
    return {
        "contract_version": GENERATION_ATTEMPT_CONTRACT_VERSION,
        "attempt_id": attempt_id,
        "attempt_kind": attempt_kind,
        "finding_attempt_index": finding_attempt_index,
        "question_revision_index": question_revision_index,
        "parent_attempt_id": parent_attempt_id,
        "parent_item_id": parent_item_id,
        "trigger_reason_code": trigger_reason_code,
        "finding_policy_version": (
            f"{generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION}:finding-"
            f"{finding_attempt_index}"
        ),
        "excluded_finding_span_ids": sorted(set(excluded_finding_span_ids)),
        # audit 4.6 d: the numeric rule repair is orthogonal to the layer a
        # rewrite answers, so it rides along with whatever repair the remaining
        # codes earn instead of competing with them for the one rung.
        "repair_numeric_rule": bool(repair_numeric_rule),
        # audit 4.6 c: the verbatim sentence one slot_lookup call found in the
        # text the writer sees, after the answer-leak filter. None when no
        # lookup ran or when the source states no such sentence.
        "slot_lookup": slot_lookup,
    }


def _path_key(attempt: dict[str, Any]) -> tuple[int, int]:
    return (
        int(attempt["finding_attempt_index"]),
        int(attempt["question_revision_index"]),
    )


def _path_sort_key(path: dict[str, Any]) -> tuple[int, int, str]:
    attempt = path["attempt"]
    return (*_path_key(attempt), str(attempt["attempt_id"]))


def _validate_slot_lookup(value: Any) -> None:
    """Check the one verbatim slot sentence a repair may carry to the writer."""
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"slot", "quote", "span_id"}:
        raise ValueError("the generation attempt slot lookup record is malformed")
    if value["slot"] not in set(_SLOT_REASON_TYPES.values()):
        raise ValueError("the generation attempt slot lookup names no known slot")
    if not isinstance(value["quote"], str) or not value["quote"].strip():
        raise ValueError("the generation attempt slot lookup quote is empty")
    if not isinstance(value["span_id"], str) or not value["span_id"]:
        raise ValueError("the generation attempt slot lookup span is missing")


def _validate_generation_attempt(attempt: Any) -> dict[str, Any]:
    fields = {
        "contract_version",
        "attempt_id",
        "attempt_kind",
        "finding_attempt_index",
        "question_revision_index",
        "parent_attempt_id",
        "parent_item_id",
        "trigger_reason_code",
        "finding_policy_version",
        "excluded_finding_span_ids",
        "repair_numeric_rule",
        "slot_lookup",
    }
    if not isinstance(attempt, dict) or set(attempt) != fields:
        raise ValueError("the generation attempt object is malformed")
    if type(attempt["repair_numeric_rule"]) is not bool:
        raise ValueError("the generation attempt numeric repair flag is invalid")
    _validate_slot_lookup(attempt["slot_lookup"])
    if attempt["contract_version"] != GENERATION_ATTEMPT_CONTRACT_VERSION:
        raise ValueError("the generation attempt contract version is unsupported")
    finding_index = attempt["finding_attempt_index"]
    revision_index = attempt["question_revision_index"]
    if (
        type(finding_index) is not int
        or type(revision_index) is not int
        or not 1 <= finding_index <= MAX_FINDING_ATTEMPTS
        or not 0 <= revision_index <= MAX_QUESTION_REVISIONS
    ):
        raise ValueError("the generation attempt indexes are invalid")
    trigger = attempt.get("trigger_reason_code")
    if (finding_index, revision_index) == (1, 0):
        allowed_kinds = {"primary"}
    elif (finding_index, revision_index) == (2, 0):
        allowed_kinds = {"alternative_finding"}
    elif revision_index in {1, 2} and trigger in OPTION_REPAIR_REASONS:
        allowed_kinds = {"option_repair"}
    elif revision_index in {1, 2} and trigger in ANSWER_RULE_REPAIR_REASONS:
        allowed_kinds = {"answer_rule_repair"}
    elif revision_index in {1, 2}:
        allowed_kinds = set(generation_contract.QUESTION_REPAIR_KINDS) | {
            "frozen_scope_rebind"
        }
    else:
        allowed_kinds = set()
    if attempt["attempt_kind"] not in allowed_kinds:
        raise ValueError("the generation attempt kind is inconsistent")
    if not isinstance(attempt["attempt_id"], str) or not attempt["attempt_id"]:
        raise ValueError("the generation attempt ID is missing")
    if not isinstance(attempt["finding_policy_version"], str):
        raise ValueError("the generation finding policy is missing")
    if not isinstance(attempt["excluded_finding_span_ids"], list) or any(
        not isinstance(span_id, str) or not span_id
        for span_id in attempt["excluded_finding_span_ids"]
    ):
        raise ValueError("the excluded finding span IDs are invalid")
    if attempt["attempt_kind"] == "primary":
        if any(
            attempt[field] is not None
            for field in ("parent_attempt_id", "parent_item_id", "trigger_reason_code")
        ):
            raise ValueError("the primary generation attempt has a parent")
    elif not isinstance(attempt["parent_attempt_id"], str) or not isinstance(
        attempt["trigger_reason_code"], str
    ):
        raise ValueError("the non-primary generation attempt lineage is incomplete")
    if attempt["parent_item_id"] is not None and not isinstance(
        attempt["parent_item_id"], str
    ):
        raise ValueError("the generation parent item ID is invalid")
    return attempt


def _candidate_generation_attempt(
    candidate: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    value = (candidate.get("provenance") or {}).get("generation_attempt")
    if value is None:
        return fallback
    return _validate_generation_attempt(value)


def _is_current_contract_candidate(candidate: dict[str, Any]) -> bool:
    provenance = candidate.get("provenance") or {}
    if (
        candidate.get("schema_version") != generation_contract.CANDIDATE_SCHEMA_VERSION
        or provenance.get("prompt_version") != generation_contract.PROMPT_VERSION
        or provenance.get("question_verification_contract_version")
        != generation_contract.QUESTION_VERIFICATION_CONTRACT_VERSION
        or provenance.get("standalone_verification_contract_version")
        != generation_contract.STANDALONE_VERIFICATION_CONTRACT_VERSION
        or provenance.get("generation_attempt_contract_version")
        != generation_contract.GENERATION_ATTEMPT_CONTRACT_VERSION
        or provenance.get("answer_agreement_contract_version")
        != generation_contract.ANSWER_AGREEMENT_CONTRACT_VERSION
        or provenance.get("numeric_rule_contract_version")
        != generation_contract.NUMERIC_RULE_CONTRACT_VERSION
        or provenance.get("direct_value_contract_version")
        != generation_contract.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
        or provenance.get("scope_contract_version")
        != generation_contract.SCOPE_CONTRACT_VERSION
        or provenance.get("scope_role_semantics_version")
        != generation_contract.SCOPE_ROLE_SEMANTICS_VERSION
        or provenance.get("scope_role_binding_contract_version")
        != generation_contract.SCOPE_ROLE_BINDING_CONTRACT_VERSION
        or provenance.get("evidence_combination_contract_version")
        != generation_contract.EVIDENCE_COMBINATION_CONTRACT_VERSION
    ):
        return False
    attempt = provenance.get("generation_attempt")
    if attempt is None:
        return candidate.get("finding_policy_version") == (
            generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
        )
    if not isinstance(attempt, dict) or attempt.get("contract_version") != (
        GENERATION_ATTEMPT_CONTRACT_VERSION
    ):
        return False
    _validate_generation_attempt(attempt)
    return candidate.get("finding_policy_version") in {
        generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION,
        attempt["finding_policy_version"],
    }


def _is_predecessor_contract_candidate(candidate: dict[str, Any]) -> bool:
    provenance = candidate.get("provenance") or {}
    attempt = provenance.get("generation_attempt")
    return bool(
        candidate.get("schema_version") == "2.5.0"
        and provenance.get("prompt_version") == "arctic-qa-generation-v20"
        and isinstance(attempt, dict)
        and attempt.get("contract_version") == "bounded-paper-progression-v2"
        and type(attempt.get("finding_attempt_index")) is int
        and type(attempt.get("question_revision_index")) is int
    )


def _generation_paths(
    db: Database,
    *,
    campaign_id: str,
    source_id: str,
    family_id: str,
) -> dict[tuple[int, int], dict[str, Any]]:
    paths: dict[tuple[int, int], dict[str, Any]] = {}
    predecessor_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    # A call record is a receipt for one generation call, not a benchmark item,
    # so it never enters path reconstruction, acceptance, or the run counts.
    rows = db.rows(
        f"""SELECT item_id,status,candidate_json FROM candidates
        WHERE run_id=? AND source_id=? AND paper_family_id=?
        AND {BENCHMARK_CANDIDATE_PREDICATE}
        ORDER BY updated_at,item_id""",
        (campaign_id, source_id, family_id),
    )
    for row in rows:
        candidate = json.loads(row["candidate_json"])
        if not _is_current_contract_candidate(candidate):
            if _is_predecessor_contract_candidate(candidate):
                predecessor_rows.append((row, candidate))
            continue
        attempt_value = (candidate.get("provenance") or {}).get("generation_attempt")
        legacy = attempt_value is None
        attempt = (
            _generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                finding_attempt_index=1,
                question_revision_index=0,
                attempt_kind="primary",
                parent_attempt_id=None,
                parent_item_id=None,
                trigger_reason_code=None,
                excluded_finding_span_ids=[],
            )
            if legacy
            else _validate_generation_attempt(attempt_value)
        )
        _validate_attempt_identity(attempt, campaign_id, family_id)
        key = _path_key(attempt)
        if key in paths:
            raise ValueError("duplicate generation attempt path")
        paths[key] = {
            "attempt": attempt,
            "candidate": row,
            "candidate_status": row["status"],
            "legacy": legacy,
        }

    if any(
        row["status"] == "machine_accepted_unverified" for row, _ in predecessor_rows
    ):
        predecessor_rows = []
    for row, candidate in predecessor_rows:
        old_attempt = candidate["provenance"]["generation_attempt"]
        finding_index = int(old_attempt["finding_attempt_index"])
        revision_index = int(old_attempt["question_revision_index"])
        key = (finding_index, revision_index)
        if (
            key in paths
            or finding_index not in {1, 2}
            or revision_index not in {0, 1, 2}
        ):
            continue
        paths[key] = {
            "attempt": old_attempt,
            "candidate": row,
            "candidate_status": row["status"],
            "legacy": False,
            "predecessor_contract": old_attempt["contract_version"],
        }

    for row in db.rows(
        """SELECT rejection_id,stage,reason_code,detail_json FROM rejection_ledger
        WHERE source_id=? AND stage IN ('generation','generation_budget')
        ORDER BY rejection_id""",
        (source_id,),
    ):
        detail = json.loads(row["detail_json"])
        if detail.get("campaign_id") != campaign_id:
            continue
        attempt_value = detail.get("generation_attempt")
        if attempt_value is None or not isinstance(attempt_value, dict):
            continue
        if attempt_value.get("contract_version") != GENERATION_ATTEMPT_CONTRACT_VERSION:
            continue
        attempt = _validate_generation_attempt(attempt_value)
        _validate_attempt_identity(attempt, campaign_id, family_id)
        key = _path_key(attempt)
        if key in paths:
            raise ValueError("generation attempt has both candidate and rejection")
        paths[key] = {
            "attempt": attempt,
            "candidate": None,
            "candidate_status": None,
            "rejection": {"reason_codes": [row["reason_code"]]},
            "budget_stop": row["stage"] == "generation_budget",
        }

    finding_policy_prefix = generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
    for row in db.rows(
        """SELECT finding_id,selection_policy_version,answer_json FROM findings
        WHERE run_id=? AND paper_family_id=? AND source_id=?
        ORDER BY selection_policy_version,finding_id""",
        (campaign_id, family_id, source_id),
    ):
        policy = row["selection_policy_version"]
        prefix = f"{finding_policy_prefix}:finding-"
        if not isinstance(policy, str) or not policy.startswith(prefix):
            continue
        try:
            finding_index = int(policy.removeprefix(prefix))
        except ValueError:
            continue
        if finding_index not in {1, 2}:
            raise ValueError("the finding policy index is outside the bounded range")
        parent_path = max(
            (
                path
                for path in paths.values()
                if path["attempt"]["finding_attempt_index"] == 1
            ),
            key=_path_sort_key,
            default=None,
        )
        attempt = _generation_attempt(
            campaign_id=campaign_id,
            family_id=family_id,
            finding_attempt_index=finding_index,
            question_revision_index=0,
            attempt_kind=("primary" if finding_index == 1 else "alternative_finding"),
            parent_attempt_id=(
                parent_path["attempt"]["attempt_id"]
                if parent_path
                else None
                if finding_index == 2
                else None
            ),
            parent_item_id=(
                parent_path["candidate"].get("item_id")
                if finding_index == 2
                and parent_path is not None
                and parent_path.get("candidate") is not None
                else None
            ),
            trigger_reason_code=(
                _path_failure(db, parent_path)["reason_codes"][0]
                if finding_index == 2 and parent_path is not None
                else "generation_rejected"
                if finding_index == 2
                else None
            ),
            excluded_finding_span_ids=(
                _prior_finding_span_ids(paths) if finding_index == 2 else []
            ),
        )
        _validate_attempt_identity(attempt, campaign_id, family_id)
        key = _path_key(attempt)
        if key in paths:
            paths[key]["finding"] = row
            paths[key]["partial_finding"] = False
            continue
        paths[key] = {
            "attempt": attempt,
            "candidate": None,
            "candidate_status": None,
            "finding": row,
            "partial_finding": True,
        }
    _validate_generation_lineage(paths)
    return paths


def _validate_generation_lineage(paths: dict[tuple[int, int], dict[str, Any]]) -> None:
    if len(paths) > MAX_CANDIDATE_PATHS:
        raise ValueError("the paper exceeds the bounded generation path limit")
    for key, path in paths.items():
        attempt = path["attempt"]
        if key == (1, 0):
            continue
        parent = next(
            (
                other
                for other in paths.values()
                if other["attempt"]["attempt_id"] == attempt["parent_attempt_id"]
            ),
            None,
        )
        if parent is None:
            raise ValueError("the generation attempt parent is missing")
        if attempt["parent_item_id"] is not None:
            candidate = parent.get("candidate")
            if candidate is None or candidate["item_id"] != attempt["parent_item_id"]:
                raise ValueError("the generation attempt parent item is inconsistent")
        if key[1] in {1, 2} and _path_key(parent["attempt"])[0] != key[0]:
            raise ValueError("a question revision changed its finding")
        if key == (2, 0) and _path_key(parent["attempt"]) == (2, 0):
            raise ValueError("the alternative finding parent is invalid")


def _accepted_generation_path(
    paths: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any] | None:
    for path in sorted(paths.values(), key=_path_sort_key):
        if path.get("predecessor_contract"):
            continue
        candidate = path.get("candidate")
        validation = path.get("validation")
        if candidate is None or validation is None:
            continue
        if validation["final_label"] == "machine_accepted_unverified" and validation[
            "labels"
        ].get("mcq_eligible"):
            return path
        if candidate["status"] == "machine_accepted_unverified" and validation[
            "labels"
        ].get("mcq_eligible"):
            return path
    return None


def _path_failure(db: Database, path: dict[str, Any]) -> dict[str, list[str]]:
    candidate = path.get("candidate")
    if candidate is not None:
        event = _require_validation_event(db, candidate)
        reasons = _reason_codes(event)
        if not reasons and candidate["status"] == "incomplete_non_mcq":
            reasons = ["insufficient_verified_distractors"]
        path["candidate_status"] = candidate["status"]
        return {"reason_codes": reasons or ["validation_rejected"]}
    rejection = path.get("rejection") or {}
    reasons = rejection.get("reason_codes") or ["generation_rejected"]
    return {"reason_codes": [str(reason) for reason in reasons]}


# Words that end in "s" and are not the plural noun of a counted sample.
_NOT_A_COUNTED_NOUN = frozenset(
    {"was", "is", "has", "its", "this", "thus", "less", "plus", "versus", "across"}
)
# The closed noun list scored false on all 7 families whose repair demanded a
# sample, and the demand became structurally unmeetable (chapter 2 yield audit
# 4.6 c, finding R1). These patterns fail open on purpose: a false "the source
# states it" only spends a rewrite the pipeline would have spent anyway, and a
# false "it is unavailable" abandons a finding the source can still support.
_SLOT_EVIDENCE_PATTERNS = {
    "period": re.compile(
        r"\b(?:1[89]\d{2}|20\d{2})\b|\b(?:January|February|March|April|May|June|"
        r"July|August|September|October|November|December)\b|"
        r"\b(?:spring|summer|autumn|winter|melt season|freeze[ -]?up|"
        r"open[ -]water season|ice[ -]free season|growing season|field season|"
        r"cruise|expedition|campaign|deployment|overwintering)\b",
        re.IGNORECASE,
    ),
    "sample": re.compile(
        r"\bn\s*=\s*\d+|"
        r"\b(?:cohort|transect|census|survey|study population|sampling campaign)\b|"
        r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen|twenty|thirty|forty|fifty)\s+"
        r"(?!(?:%s)\b)[A-Za-z][A-Za-z-]{2,}s\b" % "|".join(sorted(_NOT_A_COUNTED_NOUN)),
        re.IGNORECASE,
    ),
    "acronym": re.compile(r"\([A-Z][A-Za-z0-9-]{1,}\)|\b[A-Z]{2,}\b"),
}


_COORDINATE_PATTERN = re.compile(r"\b\d{1,2}(?:\.\d+)?\s*[\u00b0]?\s*[NS]\b")
_PROPER_NOUN_PATTERN = re.compile(r"\b[A-Z][a-z\u00c0-\u024f]{2,}\b")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _states_a_place(texts: list[str]) -> bool:
    """Say whether the study-setting text names a place at all.

    The test is deliberately wide. A false "the source states a place" only
    spends a rewrite the pipeline would have spent anyway. A false "no place"
    would discard a finding the source can still support, so a bare proper noun
    counts. A sentence's first word is skipped, because every sentence starts
    with a capital.
    """
    for text in texts:
        if _COORDINATE_PATTERN.search(text):
            return True
        for sentence in _SENTENCE_SPLIT.split(text):
            words = sentence.split()
            if any(
                _PROPER_NOUN_PATTERN.fullmatch(word.strip(",;:()"))
                for word in words[1:]
            ):
                return True
    return False


def _slot_evidence_types(texts: list[str]) -> frozenset[str]:
    """Return the referent slot kinds the study-setting text can supply."""
    joined = "\n".join(texts)
    slots = {
        slot
        for slot, pattern in _SLOT_EVIDENCE_PATTERNS.items()
        if pattern.search(joined)
    }
    if _states_a_place(texts):
        slots.add("place")
    return frozenset(slots)


def _failed_candidate(failed_path: dict[str, Any]) -> dict[str, Any] | None:
    """Return the stored candidate document of one failed path, if it has one."""
    row = failed_path.get("candidate")
    if row is None:
        return None
    try:
        candidate = json.loads(row["candidate_json"])
    except (TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None
    return candidate if isinstance(candidate, dict) else None


def _writer_visible_texts(candidate: dict[str, Any]) -> list[str]:
    """Return the hash-bound text the writer itself received for this attempt.

    The chapter 2 slot guard read ``eligible_activity_spans`` only, which the
    eligibility screen picked for another purpose, so the router judged a demand
    met or unmeetable from text the writer never saw (audit 4.6 c, finding R1).
    """
    answer = candidate.get("answer")
    answer = answer if isinstance(answer, dict) else {}
    texts = [str(answer.get("evidence_quote") or "")]
    for component in answer.get("evidence_components") or []:
        if isinstance(component, dict):
            texts.append(str(component.get("text") or ""))
        elif isinstance(component, str):
            texts.append(component)
    for span in validation_contract.context_only_span_records(
        candidate.get("provenance")
    ):
        texts.append(str(span.get("text") or ""))
    return [text for text in texts if text.strip()]


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _routing_evidence(failed_path: dict[str, Any]) -> dict[str, Any]:
    """Collect the structured records routing may read from a failed candidate.

    Routing reads typed diagnostics, the writer's own payload and the
    deterministic scope check only. It never reads a judge's free text, which is
    the same rule ``ATTEMPT_HISTORY`` keeps.
    """
    candidate = _failed_candidate(failed_path)
    if candidate is None:
        return {"has_candidate": False}
    standalone = candidate.get("standalone_verification")
    standalone = standalone if isinstance(standalone, dict) else {}
    verification = candidate.get("answer_verification")
    verification = verification if isinstance(verification, dict) else {}
    agreement = candidate.get("answer_agreement")
    return {
        "has_candidate": True,
        "unresolved_phrases": _text_list(standalone.get("unresolved_phrases")),
        "missing_detail_types": _text_list(standalone.get("missing_detail_types")),
        "scope_defect": validation_contract.scope_defect_records(candidate),
        "standalone_pass": standalone.get("pass"),
        "referent_resolved": verification.get("question_context_referent_resolved"),
        "missing_detail": str(
            verification.get("question_context_missing_detail") or ""
        ).strip(),
        "residual_error": str(verification.get("residual_error") or "").strip(),
        "relation_scope_match": verification.get("relation_scope_match"),
        "scope_contradicted": verification.get("scope_value_contradicted_by_source"),
        "agreement": agreement if isinstance(agreement, dict) else {},
        "writer_texts": _writer_visible_texts(candidate),
    }


def _slot_evidence_pool(
    db: Database, source_id: str, evidence: dict[str, Any] | None
) -> frozenset[str] | None:
    """Return the slot kinds the text the writer will see can supply.

    The pool is SOURCE_DATA plus CONTEXT_ONLY_SOURCE plus the eligibility
    activity spans, never the activity spans alone.
    """
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    quotes = list(generation_contract.eligible_activity_spans(source)) if source else []
    texts = [*((evidence or {}).get("writer_texts") or []), *quotes]
    if not texts:
        return None
    return _slot_evidence_types(texts)


def _agreement_verdict_is_authoritative(agreement: dict[str, Any]) -> bool:
    """Say whether a reconstruction disagreement may buy a repair cycle.

    A disagreement counts from the deterministic comparator or from a judge the
    role contract ranks at the Pro tier. The flash-lite judge answered "no" to
    "24 species" against "24" and bought a whole repair cycle for family
    2fa3406e (audit 4.6 f, finding R6). The bar is on a judge the roster ranks
    below that tier, and only on one: a record the roster cannot rank is left
    alone, because the rule removes a known-weak verdict and invents no new
    reason to stop.
    """
    if not isinstance(agreement, dict) or agreement.get("method") != "llm_judge":
        return True
    judge = agreement.get("judge")
    if not isinstance(judge, dict):
        return True
    model = judge.get("requested_model") or judge.get("returned_model")
    try:
        strength = load_role_contract()["model_strength_rank"]
    except (ValueError, OSError):
        return True
    rank = strength.get(str(model))
    if not isinstance(rank, int):
        return True
    return rank >= MINIMUM_AGREEMENT_TRIGGER_STRENGTH


def _authoritative_reason_codes(
    reason_codes: list[str], evidence: dict[str, Any] | None
) -> list[str]:
    """Drop a trigger whose only evidence is a judge too weak to spend on."""
    if "reconstruction_disagreement" not in reason_codes:
        return reason_codes
    if evidence is None or not evidence.get("has_candidate"):
        return reason_codes
    if _agreement_verdict_is_authoritative(evidence.get("agreement") or {}):
        return reason_codes
    return [code for code in reason_codes if code != "reconstruction_disagreement"]


# A rewrite that is handed no phrase, no detail type and no scope field has
# nothing to act on. The guard applies only where the repair would be a
# question-level rewrite: an option repair reuses the verified question, a
# finding-layer code leaves the finding, and a leakage code names its own fix.
_DIAGNOSTIC_REQUIRED_LAYERS = frozenset({"context", "evidence", "contract"})
# Codes whose own contract promises the displayed words they object to, and
# whose diagnostic this router can read. A self-describing code such as
# question_context_missing names its own fix and is never suppressed. So is a
# code whose diagnostic lives on a record the router does not compute: the
# replay over the 139 chapter 2 candidates showed that
# reconstruction_scope_not_source_bound and question_qualifier_not_evidence_bound
# reach a context widening that accepted an item (family e02e286a), and the
# guard must never take that path away. Widen this set only with a code whose
# diagnostic _routing_evidence actually carries.
DIAGNOSTIC_BEARING_REASONS = frozenset(
    {
        # the answer record's own scope, which scope_defect_records computes
        "relation_scope_mismatch",
        "scope_qualifier_missing",
        "scope_qualifier_not_displayed",
        "scope_qualifier_not_source_bound",
        "scope_value_not_source_supported",
        "answer_scope_not_source_bound",
        "answer_verifier_scope_not_source_bound",
        # the source-blind judge carries unresolved_phrases and
        # missing_detail_types for every one of these
        "standalone_undefined_subject_or_system",
        "standalone_undefined_measured_variable",
        "standalone_undefined_unit_meaning",
        "standalone_undefined_percentage_basis",
        "standalone_undefined_acronym",
        "standalone_undefined_location",
        "standalone_undefined_period_or_event",
        "standalone_undefined_population_or_sample",
        "standalone_undefined_treatment_or_condition",
        "standalone_undefined_comparison_basis",
        "standalone_unresolved_study_local_referent",
        "standalone_source_dependent_locator",
    }
)


def _unroutable_outcome(
    reason_codes: list[str], evidence: dict[str, Any] | None
) -> str | None:
    """Return the terminal outcome when a rewrite has nothing to act on.

    45 of 139 chapter 2 candidates were rejected with an empty
    ``unresolved_phrases`` and an empty ``missing_detail_types``; 29 of them were
    repairs, which cost USD 1.26 and returned no accepted item (audit 4.6 b,
    finding R2). The condition reads whatever judge records exist at rejection
    time: an absent record is not evidence of a contradiction.
    """
    if evidence is None or not evidence.get("has_candidate"):
        return None
    if not reason_codes:
        return None
    if _primary_failure_layer(reason_codes) not in _DIAGNOSTIC_REQUIRED_LAYERS:
        return None
    if not all(reason in DIAGNOSTIC_BEARING_REASONS for reason in reason_codes):
        return None
    if (
        evidence.get("unresolved_phrases")
        or evidence.get("missing_detail_types")
        or evidence.get("scope_defect")
        or evidence.get("missing_detail")
        or evidence.get("residual_error")
    ):
        return None
    judges_clean = (
        evidence.get("standalone_pass") is not False
        and evidence.get("referent_resolved") is not False
        and evidence.get("relation_scope_match") is not False
        and evidence.get("scope_contradicted") is not True
    )
    return (
        "gate_contradiction_unroutable"
        if judges_clean
        else "empty_diagnostic_unroutable"
    )


def _primary_failure_layer(reason_codes: list[str]) -> str:
    """Pick the one layer a repair must act on when several layers report."""
    layers = {_failure_layer(reason) for reason in reason_codes}
    for layer in _LAYER_PRIORITY:
        if layer in layers:
            return layer
    return "contract"


def _reason_family(reason: str) -> str:
    """Collapse a reason code to the demand a repair would answer."""
    if reason.startswith(("standalone_undefined_", "standalone_unresolved_")):
        return "referent_slot"
    # Cost slice (audit 4.4): a writer-declared slot gap is the same demand.
    if reason in generation_contract.WRITER_SLOT_UNAVAILABLE_REASONS:
        return "referent_slot"
    if reason.startswith("standalone_"):
        return "standalone"
    if reason.startswith("question_context_"):
        return "question_context"
    if reason in SCOPE_FAMILY_REASONS:
        return "scope"
    return reason


# Rungs the lineage-wide counter never suppresses. ``option_repair`` reuses the
# parent's already verified question and went 5 for 6 in chapter 2, so a repeat
# on the option demand is the cheapest item in the pipeline, not a waste.
REPEAT_EXEMPT_REPAIR_KINDS = frozenset({"option_repair"})


def _repeat_depth(paths: dict[tuple[int, int], dict[str, Any]], family: str) -> int:
    """Count earlier attempts in the whole lineage that answered this demand.

    The chapter 2 counter reset on a finding switch, so 72 of 139 candidates
    re-failed on a defect their own family had already seen (audit 4.6 e). The
    counter now spans the family lineage. An exempt rung is not counted, so a
    repeat on the option demand still earns its rung.
    """
    return sum(
        1
        for path in paths.values()
        if isinstance(path["attempt"].get("trigger_reason_code"), str)
        and _reason_family(str(path["attempt"]["trigger_reason_code"])) == family
        and path["attempt"].get("attempt_kind") not in REPEAT_EXEMPT_REPAIR_KINDS
    )


def _failed_candidate(path: dict[str, Any]) -> dict[str, Any] | None:
    candidate = path.get("candidate")
    if not isinstance(candidate, dict):
        return None
    payload = candidate.get("candidate_json")
    if isinstance(payload, str):
        try:
            loaded = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return candidate


def _failed_answer_scope(path: dict[str, Any]) -> dict[str, Any] | None:
    """Return the frozen answer scope of the failed candidate, or None."""
    candidate = _failed_candidate(path)
    if candidate is None:
        return None
    scope = (candidate.get("answer") or {}).get("scope")
    return scope if isinstance(scope, dict) else None


def _forwarded_slot_evidence(path: dict[str, Any]) -> frozenset[str] | None:
    """Return the slot kinds of the text the writer saw, or None when unknown.

    ch2 yield audit 4.2 (F3) and 4.6 (c): the guard reads the forwarded
    context-only spans and the frozen finding's own evidence quote, not the
    raw eligibility spans. None disables the guard rather than guessing.
    """
    candidate = _failed_candidate(path)
    if candidate is None:
        return None
    texts = [
        str(span.get("text", ""))
        for span in generation_contract.context_only_span_records(
            candidate.get("provenance")
        )
        if isinstance(span.get("text"), str)
    ]
    evidence = (candidate.get("answer") or {}).get("evidence_quote")
    if isinstance(evidence, str) and evidence:
        texts.append(evidence)
    if not texts:
        return None
    return _slot_evidence_types(texts)


def _slot_demand_unmet(
    reason_codes: list[str], slot_evidence: frozenset[str] | None
) -> bool:
    """Say whether the repair would demand a slot the source cannot supply."""
    if slot_evidence is None:
        return False
    demanded = {
        _SLOT_REASON_TYPES[reason]
        for reason in reason_codes
        if reason in _SLOT_REASON_TYPES
    }
    return bool(demanded) and not (demanded & slot_evidence)


def _unmet_slot_demands(
    reason_codes: list[str], slot_evidence: frozenset[str] | None
) -> frozenset[str]:
    """Return the referent slots the writer-visible text cannot supply."""
    if slot_evidence is None:
        return frozenset()
    demanded = {
        _SLOT_REASON_TYPES[reason]
        for reason in _routing_reason_codes(reason_codes)
        if reason in _SLOT_REASON_TYPES
    }
    if not demanded or demanded & slot_evidence:
        return frozenset()
    return frozenset(demanded)


def _run_slot_lookup(
    db: Database,
    author: Provider,
    *,
    run_id: str,
    failed_path: dict[str, Any],
    evidence: dict[str, Any],
    source_id: str,
    slot: str,
) -> dict[str, Any] | None:
    """Ask once for the sentence that states a slot the patterns could not find.

    A pattern miss is a reason to ask once, not a reason to abandon a finding.
    The chapter 2 guard abandoned family 7ad42191 over a year that sat inside
    the admitted finding span, skipped a USD 0.0066 correction and paid USD 0.20
    for two extractions that froze nothing (audit 4.6 c, finding R1).
    """
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    quotes = list(generation_contract.eligible_activity_spans(source)) if source else []
    texts = [*(evidence.get("writer_texts") or []), *quotes]
    if not texts:
        return None
    candidate = _failed_candidate(failed_path) or {}
    answer = candidate.get("answer")
    broker = getattr(author, "broker", None)
    timeout = (
        maximum_call_timeout_seconds(broker.config)
        if broker is not None
        else DEFAULT_CALL_TIMEOUT_SECONDS
    )
    try:
        return generation_contract.slot_lookup_quote(
            db,
            author,
            run_id=run_id,
            entity_id=stable_id(
                "slot-lookup",
                run_id,
                failed_path["attempt"]["attempt_id"],
                slot,
            ),
            slot=slot,
            texts=texts,
            answer=answer if isinstance(answer, dict) else None,
            parameters={
                "temperature": 0,
                "max_tokens": 512,
                "reasoning_token_cap": 0,
                "billable_token_overhead": 0,
            },
            reservation=Decimal("1"),
            timeout=timeout,
            retries=0,
            rate_limit_seconds=0,
        )
    except (CandidateRejectedError, ProviderResponseError):
        # The lookup is an optional cheap retrieval. A failure routes the slot
        # as unavailable, exactly as a pattern miss did before it existed.
        return None


def _alternative_finding_attempt(
    *,
    campaign_id: str,
    family_id: str,
    paths: dict[tuple[int, int], dict[str, Any]],
    failed_attempt: dict[str, Any],
    failed_path: dict[str, Any],
    trigger_reason_code: str,
    require_exclusions: bool = True,
) -> dict[str, Any] | None:
    if (2, 0) in paths:
        return None
    excluded = _prior_finding_span_ids(paths)
    if require_exclusions and not excluded:
        return None
    return _generation_attempt(
        campaign_id=campaign_id,
        family_id=family_id,
        finding_attempt_index=2,
        question_revision_index=0,
        attempt_kind="alternative_finding",
        parent_attempt_id=failed_attempt["attempt_id"],
        parent_item_id=(failed_path.get("candidate") or {}).get("item_id"),
        trigger_reason_code=trigger_reason_code,
        excluded_finding_span_ids=excluded,
    )


def _next_generation_attempt(
    *,
    campaign_id: str,
    family_id: str,
    paths: dict[tuple[int, int], dict[str, Any]],
    failed_path: dict[str, Any],
    reason_codes: list[str],
    slot_evidence: frozenset[str] | None = None,
    forwarded_slot_evidence: frozenset[str] | None = None,
    evidence: dict[str, Any] | None = None,
    unroutable: list[str] | None = None,
) -> dict[str, Any] | None:
    """Choose the one repair this failure earns, inside the six-path bound.

    v21 returned None whenever the collapsed codes spanned more than one failure
    layer, which is the normal case, so 30 of 43 families ended with 128 of 180
    budgeted paths unspent (r15 audit section 4.6). Routing now repairs the
    highest-priority layer only. It decides which repair runs, never whether an
    item is accepted: every repaired candidate re-runs the whole gate sequence
    and consumes a path.

    ``evidence`` carries the structured records of the failed candidate, from
    ``_routing_evidence``. ``unroutable`` collects the terminal outcome code when
    routing stops because a rewrite has nothing to act on, so the caller can
    record it and flag the family for gate review.
    """
    reason_codes = _routing_reason_codes(reason_codes)
    reason_codes = _authoritative_reason_codes(reason_codes, evidence)
    if not reason_codes:
        return None
    # audit 4.6 d: the numeric rule repair is a cost swap, not a competitor for
    # the one rung. It is stripped before layer selection and runs inside the
    # same attempt as whatever repair the remaining codes earn.
    repair_numeric_rule = "source_bound_numeric_rule_missing" in reason_codes
    if repair_numeric_rule:
        remaining = [
            reason
            for reason in reason_codes
            if reason != "source_bound_numeric_rule_missing"
        ]
        if remaining:
            reason_codes = remaining
    outcome = _unroutable_outcome(reason_codes, evidence)
    if outcome is not None:
        if unroutable is not None:
            unroutable.append(outcome)
        return None
    layer = _primary_failure_layer(reason_codes)
    primary = [reason for reason in reason_codes if _failure_layer(reason) == layer]
    reason_codes = primary or reason_codes
    scope_defects = list((evidence or {}).get("scope_defect") or [])
    failed_attempt = failed_path["attempt"]
    finding_index = int(failed_attempt["finding_attempt_index"])
    revision_index = int(failed_attempt["question_revision_index"])
    trigger = reason_codes[0]
    slot_lookup = (evidence or {}).get("slot_lookup")

    if finding_index == 1 and (
        (len(reason_codes) == 1 and trigger in IMMEDIATE_ALTERNATIVE_FINDING_REASONS)
        or layer == "finding"
    ):
        alternative = _alternative_finding_attempt(
            campaign_id=campaign_id,
            family_id=family_id,
            paths=paths,
            failed_attempt=failed_attempt,
            failed_path=failed_path,
            trigger_reason_code=trigger,
        )
        if alternative is not None:
            return alternative

    # A demand is only spent when the source can meet it. When the paper states
    # no place, period, sample size, or acronym expansion of the demanded kind,
    # a rewrite can only invent filler, so the family moves to another finding.
    # ch2 yield audit 4.2 (F3 as amended): the satisfiability guard is a pure
    # function in generation.py. It reads the frozen answer scope and the slot
    # kinds of the text the writer saw, never the raw eligibility spans.
    unsatisfiable = generation_contract.unsatisfiable_standalone_demands(
        reason_codes,
        answer_scope=_failed_answer_scope(failed_path),
        supplied_slots=forwarded_slot_evidence,
    )
    if unsatisfiable or _slot_demand_unmet(reason_codes, slot_evidence):
        if finding_index != 1:
            return None
        return _alternative_finding_attempt(
            campaign_id=campaign_id,
            family_id=family_id,
            paths=paths,
            failed_attempt=failed_attempt,
            failed_path=failed_path,
            trigger_reason_code="slot_evidence_unavailable",
        )

    repairable = bool(reason_codes) and all(
        reason in REPAIRABLE_QUESTION_REASONS
        or reason in OPTION_REPAIR_REASONS
        or reason in ANSWER_RULE_REPAIR_REASONS
        for reason in reason_codes
    )
    next_revision = revision_index + 1
    rung_declined = False
    if repairable and next_revision <= MAX_QUESTION_REVISIONS:
        key = (finding_index, next_revision)
        if key not in paths:
            depth = _repeat_depth(paths, _reason_family(trigger))
            kind = _repair_kind(
                reason_codes,
                depth,
                scope_defects=scope_defects,
                finding_has_widened=_finding_has_widened(paths, finding_index),
            )
            rung_declined = kind is None
            if kind is not None:
                return _generation_attempt(
                    campaign_id=campaign_id,
                    family_id=family_id,
                    finding_attempt_index=finding_index,
                    question_revision_index=next_revision,
                    attempt_kind=kind,
                    parent_attempt_id=failed_attempt["attempt_id"],
                    parent_item_id=(failed_path.get("candidate") or {}).get("item_id"),
                    trigger_reason_code=trigger,
                    excluded_finding_span_ids=[],
                    repair_numeric_rule=repair_numeric_rule,
                    slot_lookup=(
                        slot_lookup
                        if kind in generation_contract.QUESTION_REPAIR_KINDS
                        else None
                    ),
                )
    if finding_index != 1:
        return None
    can_leave_finding = bool(reason_codes) and all(
        reason in REPAIRABLE_QUESTION_REASONS
        or reason in ALTERNATIVE_FINDING_REASONS
        or reason in ANSWER_RULE_REPAIR_REASONS
        for reason in reason_codes
    )
    if not can_leave_finding:
        return None
    if revision_index < MAX_QUESTION_REVISIONS and not rung_declined:
        # The rewrite budget on this finding is not spent and no rung has given
        # up on it, so an alternative finding would waste a path.
        return None
    return _alternative_finding_attempt(
        campaign_id=campaign_id,
        family_id=family_id,
        paths=paths,
        failed_attempt=failed_attempt,
        failed_path=failed_path,
        trigger_reason_code=trigger,
        require_exclusions=False,
    )


def _finding_has_widened(
    paths: dict[tuple[int, int], dict[str, Any]], finding_index: int
) -> bool:
    """Say whether this finding already spent its one context widening."""
    return any(
        int(path["attempt"]["finding_attempt_index"]) == finding_index
        and path["attempt"].get("attempt_kind") == "context_widened_revision"
        for path in paths.values()
    )


def _repair_kind(
    reason_codes: list[str],
    repeat_depth: int,
    *,
    scope_defects: Sequence[dict[str, Any]] = (),
    finding_has_widened: bool = True,
) -> str | None:
    """Pick the repair rung for one primary layer at this repeat depth.

    A scope trigger is routed by the demand the deterministic check computed,
    never by the code alone. ``display_verbatim`` means the frozen value is in
    the evidence and reaches no reader, which a question rewrite can place.
    ``not_in_evidence`` means the evidence does not state it, which only a
    rebind of the frozen answer record can settle (audit 4.6 a and 4.6 d).
    """
    trigger = reason_codes[0]
    if trigger in OPTION_REPAIR_REASONS:
        return "option_repair"
    if trigger in ANSWER_RULE_REPAIR_REASONS:
        # A question rewrite cannot repair a numeric rule, and the rung is
        # allowed one attempt only.
        return "answer_rule_repair" if repeat_depth < 1 else None
    if trigger in SCOPE_FAMILY_REASONS:
        demands = {str(defect.get("demand")) for defect in scope_defects}
        if "display_verbatim" in demands:
            if repeat_depth < 1:
                return "scope_display_repair"
        elif demands == {"not_in_evidence"}:
            # A rewrite may not touch the frozen scope, so the ordinary ladder
            # would spend a path it cannot win.
            return "frozen_scope_rebind" if repeat_depth < 1 else None
    if repeat_depth >= 2:
        # The lineage-wide counter stops a demand the family has already
        # answered twice. The one exemption is the widening rung, which the
        # replay over the 139 chapter 2 candidates showed the counter would
        # otherwise suppress (audit 4.6 e). It runs once per finding, because
        # CONTEXT_ONLY_SOURCE is new text for a finding the family has not yet
        # widened, and never twice on the same one.
        return None if finding_has_widened else "context_widened_revision"
    if repeat_depth == 1:
        return "context_widened_revision"
    if len(reason_codes) == 1 and trigger in SURGICAL_CORRECTION_REASONS:
        return "surgical_correction"
    return "question_revision"


def _routing_reason_codes(reason_codes: list[str]) -> list[str]:
    """Collapse only documented downstream symptoms for one repair root."""
    normalized = list(dict.fromkeys(str(reason) for reason in reason_codes))
    specific_standalone = [
        reason for reason in normalized if reason.startswith("standalone_undefined_")
    ]
    if specific_standalone:
        correlated = {
            "standalone_gate_failed",
            "standalone_unresolved_study_local_referent",
        }
        normalized = [reason for reason in normalized if reason not in correlated]
    if any(
        reason.startswith("standalone_")
        for reason in normalized
        if reason != "standalone_answer_leakage"
    ):
        # A missing referent makes the scope, ambiguity and claim-type verdicts
        # downstream symptoms of one root (r15 audit, stage reconstruction, R4).
        # source_entailment_not_verified is never collapsed: it is the
        # paper-support signal.
        normalized = [
            reason
            for reason in normalized
            if reason not in _STANDALONE_DEPENDENT_REASONS
        ]
    roots = set(normalized).intersection(_DEPENDENT_ROUTING_REASONS)
    if not roots:
        return normalized
    dependent = set().union(*(_DEPENDENT_ROUTING_REASONS[root] for root in roots))
    return [reason for reason in normalized if reason not in dependent]


def _failure_layer(reason: str) -> str:
    if reason in {
        "standalone_answer_leakage",
        "question_answer_leakage",
        "question_context_answer_leakage",
    }:
        return "leakage"
    if reason in OPTION_REPAIR_REASONS or reason.startswith(("option_", "distractor_")):
        return "options"
    if reason.startswith("standalone_") or reason.startswith("question_context_"):
        return "context"
    # Cost slice (audit 4.4): the writer-declared slot gap is a context defect.
    if reason in generation_contract.WRITER_SLOT_UNAVAILABLE_REASONS:
        return "context"
    # Cost slice (audit 4.5 b): the extractor found only a study-internal index.
    if reason == "no_admissible_finding":
        return "finding"
    if reason in TERMINAL_GENERATION_REASONS:
        return "contract"
    if reason in SCOPE_FAMILY_REASONS or reason in {
        "benchmark_text_malformed",
        "publication_relative_period",
        "question_qualifier_not_evidence_bound",
    }:
        # audit 4.6 d: the whole scope family sits in one layer, so the numeric
        # and contract catch-alls stop winning the repair from it.
        return "context"
    if reason in UNROUTABLE_OUTCOME_REASONS:
        # Terminal routing outcomes. They are recorded, never repaired.
        return "contract"
    if reason == "slot_evidence_unavailable":
        return "finding"
    if reason in {
        "source_entailment_not_verified",
        "reconstruction_disagreement",
        "reconstruction_alternative_answer_present",
        "alternative_answer_unresolved",
    }:
        return "evidence"
    if reason in {
        # Registered for the eligibility slice (chapter 3). Both end a screening
        # attempt before any candidate exists, so the family layer records them
        # and no candidate-level rung may claim them.
        "eligible_arctic_scope_dimension_unsupported",
        "eligible_arctic_scope_phrase_not_specific",
    }:
        return "finding"
    if reason.startswith("eligible_arctic_") or reason.startswith("finding_"):
        return "finding"
    return "contract"


def _prior_finding_span_ids(paths: dict[tuple[int, int], dict[str, Any]]) -> list[str]:
    span_ids: set[str] = set()
    for path in paths.values():
        if path["attempt"]["finding_attempt_index"] != 1:
            continue
        candidate = path.get("candidate")
        if candidate is not None:
            payload = json.loads(candidate["candidate_json"])
            answer = payload.get("answer") or {}
        else:
            finding = path.get("finding")
            if finding is None:
                continue
            try:
                answer = json.loads(finding["answer_json"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
        if not isinstance(answer, dict):
            continue
        source_span_id = answer.get("source_span_id")
        if isinstance(source_span_id, str) and source_span_id:
            span_ids.add(source_span_id)
        source_span_ids = answer.get("source_span_ids")
        if isinstance(source_span_ids, list):
            span_ids.update(
                span_id
                for span_id in source_span_ids
                if isinstance(span_id, str) and span_id
            )
    return sorted(span_ids)


def _reason_codes(event: dict[str, Any]) -> list[str]:
    reasons = json.loads(event["reason_codes_json"])
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in reasons
    ):
        raise ValueError("a streaming validation event has invalid reason codes")
    return reasons


def _validation_result_from_event(
    db: Database, candidate: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    details_row = db.one(
        """SELECT details_json FROM validation_events
        WHERE item_id=? AND json_extract(details_json, '$.candidate_hash')=?
        ORDER BY created_at DESC,event_id DESC LIMIT 1""",
        (
            candidate["item_id"],
            stable_id("candidate-payload", candidate["candidate_json"]),
        ),
    )
    if details_row is None:
        raise ValueError("a streaming validation event has no payload details")
    details = json.loads(details_row["details_json"])
    labels = details.get("labels")
    if not isinstance(labels, dict):
        raise ValueError("a streaming validation event has invalid labels")
    return {
        "final_label": event["label"],
        "reasons": _reason_codes(event),
        "labels": labels,
    }


def _validate_attempt_identity(
    attempt: dict[str, Any], campaign_id: str, family_id: str
) -> None:
    expected_id = stable_id(
        "generation-attempt",
        campaign_id,
        family_id,
        attempt["finding_attempt_index"],
        attempt["question_revision_index"],
        GENERATION_ATTEMPT_CONTRACT_VERSION,
    )
    expected_policy = (
        f"{generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION}:finding-"
        f"{attempt['finding_attempt_index']}"
    )
    if (
        attempt["attempt_id"] != expected_id
        or attempt["finding_policy_version"] != expected_policy
    ):
        raise ValueError("the generation attempt identity is not deterministic")


def _require_validation_event(
    db: Database, candidate: dict[str, Any]
) -> dict[str, Any]:
    event = _validation_event_for_stored_candidate(db, candidate)
    if event is None:
        raise ValueError(
            "a terminal streaming candidate lacks a validation event for its payload"
        )
    return event


GENERATION_CALL_RECORD_CONTRACT_VERSION = "generation-call-record-v1"


def _generation_call_record_id(
    run_id: str, family_id: str, source_version_id: str, attempt_id: str
) -> str:
    """Return the idempotent key of one generation call.

    The same attempt on the same frozen source text always resolves to the same
    row, so a resumed run settles the record it already opened instead of
    opening a second one and paying twice (audit 4.9 C8).
    """
    return stable_id(
        "generation-call",
        GENERATION_CALL_RECORD_CONTRACT_VERSION,
        run_id,
        family_id,
        source_version_id,
        attempt_id,
    )


def _open_generation_call_record(
    db: Database,
    *,
    run_id: str,
    source_id: str,
    family_id: str,
    source_version_id: str,
    attempt: dict[str, Any],
) -> str:
    """Persist the in-flight candidate before any charge-uncertain call.

    Two chapter 2 families lost every completed call to an ambiguous charge and
    one family's attempt vanished with no terminal record at all (audit 4.9 C8),
    and 26 generation calls produced no row of any kind (audit 4.6 e). The row
    opens as ``incomplete_infra``: a call was started and its outcome is not yet
    known. It is a receipt for the call, never a benchmark item, so it takes no
    part in path reconstruction, acceptance or the run counts.
    """
    item_id = _generation_call_record_id(
        run_id, family_id, source_version_id, attempt["attempt_id"]
    )
    record = {
        "schema_version": generation_contract.CANDIDATE_SCHEMA_VERSION,
        "item_id": item_id,
        "generation_call_record": GENERATION_CALL_RECORD_CONTRACT_VERSION,
        "state": "incomplete_infra",
        "source": {"source_id": source_id, "paper_family_id": family_id},
        "provenance": {
            "run_id": run_id,
            "generation_attempt": attempt,
            "source_version_id": source_version_id,
            "request_identity": {
                "run_id": run_id,
                "paper_id": source_id,
                "family_id": family_id,
                "source_version_id": source_version_id,
                "attempt_id": attempt["attempt_id"],
            },
        },
    }
    with db.transaction():
        db.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'incomplete_infra',?,?)
            ON CONFLICT(item_id) DO UPDATE SET
              candidate_json=excluded.candidate_json,
              status='incomplete_infra',
              updated_at=excluded.updated_at""",
            (
                item_id,
                run_id,
                source_id,
                family_id,
                "answer_first",
                canonical_json(record),
                now(),
                now(),
            ),
        )
    return item_id


def _settle_generation_call_record(
    db: Database,
    item_id: str,
    *,
    state: str,
    reason_code: str | None = None,
    candidate_item_id: str | None = None,
) -> None:
    """Close one in-flight call record with the outcome the call reached."""
    row = _candidate_row(db, item_id)
    if row is None:
        return
    try:
        record = json.loads(row["candidate_json"])
    except (TypeError, json.JSONDecodeError):
        return
    record["state"] = state
    record["reason_code"] = reason_code
    record["candidate_item_id"] = candidate_item_id
    with db.transaction():
        db.connection.execute(
            "UPDATE candidates SET candidate_json=?,status=?,updated_at=? WHERE item_id=?",
            (canonical_json(record), state, now(), item_id),
        )


def _incomplete_infra_records(
    db: Database, run_id: str, family_id: str
) -> list[dict[str, Any]]:
    """Return the call records of one family whose outcome is still unknown."""
    return [
        dict(row)
        for row in db.rows(
            """SELECT item_id,status,candidate_json FROM candidates
            WHERE run_id=? AND paper_family_id=? AND status='incomplete_infra'
            ORDER BY item_id""",
            (run_id, family_id),
        )
    ]


def _record_gate_review_flag(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    selected: dict[str, Any],
    attempt: dict[str, Any],
    reason_code: str,
    rejection_reason_codes: list[str],
) -> None:
    """Record why routing stopped and flag the family for gate review.

    ``gate_contradiction_unroutable`` means every judge record present reports
    clean while a deterministic code killed the candidate, so the gates
    contradict each other. ``empty_diagnostic_unroutable`` means a judge named a
    defect and no diagnostic named the words it objects to. Neither can accept
    anything: they only stop the pipeline paying for a rewrite that is handed
    nothing to act on.
    """
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "selection": selected,
        "generation_attempt": attempt,
        "rejection_reason_codes": rejection_reason_codes,
        "gate_review_required": True,
        "routing_outcome": reason_code,
    }
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,?,'generation_routing',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    "generation_routing",
                    attempt["attempt_id"],
                    reason_code,
                ),
                source_id,
                reason_code,
                canonical_json(detail),
                now(),
            ),
        )


def _record_generation_rejection(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    selected: dict[str, Any],
    attempt: dict[str, Any],
    reason_code: str,
    error: Exception,
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "error": str(error),
        "selection": selected,
        "generation_attempt": attempt,
    }
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,?,'generation',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    "generation",
                    attempt["attempt_id"],
                    reason_code,
                ),
                source_id,
                reason_code,
                canonical_json(detail),
                now(),
            ),
        )


def _record_operational_unresolved(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    selected: dict[str, Any],
    family_id: str,
    request_key: str,
    reason_code: str,
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "family_id": family_id,
        "broker_request_key": request_key,
        "selection": selected,
        "replay_prohibited": True,
        "operational_unresolved": True,
    }
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,NULL,'generation',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    family_id,
                    request_key,
                    reason_code,
                ),
                reason_code,
                canonical_json(detail),
                now(),
            ),
        )


def _record_budget_stop(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    selected: dict[str, Any],
    attempt: dict[str, Any],
    error: Exception,
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "error": str(error),
        "selection": selected,
        "budget_stop": True,
        "generation_attempt": attempt,
    }
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,?,'generation_budget',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    "generation_budget",
                    attempt["attempt_id"],
                    "request_cost_bound_exceeded",
                ),
                source_id,
                "request_cost_bound_exceeded",
                canonical_json(detail),
                now(),
            ),
        )


def _family_cost_state(provider: Provider, family_id: str) -> dict[str, str] | None:
    """Return the family's committed spend, or None without a shared broker."""
    broker = getattr(provider, "broker", None)
    if broker is None:
        return None
    try:
        return broker.family_cost_state(family_id)
    except Exception:
        # The skip is an accounting note, never a second failure path: a family
        # the cap stopped is recorded even when its cost row cannot be read.
        return None


def _operational_unresolved_families(broker: Any) -> dict[str, dict[str, str]]:
    """Read the reviewed no-replay families, through the broker's own accessor.

    The read validates the shared ledger, so it can refuse. That refusal is the
    ledger's, never one paper's, and it does not cross the provider seam, so it
    is marked here.
    """
    if broker is None:
        return {}
    try:
        return broker.operational_unresolved_families()
    except Exception as error:
        raise mark_run_stop(error)


def _ends_the_run(error: BaseException) -> bool:
    """Say whether this exception is a run stop rather than one paper's fault.

    Only a run stop ends the producer. Everything a single candidate or a single
    paper family raises - a ``ValueError`` from the generation contract, a
    ``KeyError`` from a routing or option record, a persistence failure - is
    contained, settled against that family and skipped.

    A run stop arrives in one of three ways. The money stops carry their own
    class: ``BudgetError`` for the allocation ceiling, the session ceiling, the
    construction checkpoint, the submission cap and the accepted-question
    target, and ``AmbiguousChargeError`` for an unsettled charge. Every other
    refusal the shared broker raises, the execution gate and the ledger
    included, carries the marker that ``broker_provider.broker_boundary`` puts
    on it at the seam. A direct read of the shared ledger, which does not cross
    that seam, is matched by its own message. The per-paper cost cap bounds one
    family, so it is never a run stop, and neither is a bounded wait for the
    exclusive operation lock that a reviewed operation of another worker held:
    it reserved nothing and submitted nothing.
    """
    if isinstance(error, (PaperCostCapError, BrokerOperationBusyError)):
        return False
    if isinstance(error, (BudgetError, AmbiguousChargeError)) or is_run_stop(error):
        return True
    return isinstance(error, ValueError) and str(error).startswith(
        RUN_ENDING_LEDGER_STOPS
    )


def _released_family_reservation(
    provider: Provider, family_id: str
) -> dict[str, str] | None:
    """Confirm the broker holds no reservation for a family a fault stopped.

    ``execute`` is the broker's only reservation path and it settles every
    request it opens, an ambiguous charge included, so a contained fault has
    nothing left to release. This reads the family's own cost row through the
    broker's supported accessor and refuses to contain the fault while that row
    still shows a reservation: stranded money is settled through the reviewed
    settlement path, never by the producer skipping past it.
    """
    broker = getattr(provider, "broker", None)
    if broker is None:
        return None
    state = broker.family_cost_state(family_id)
    if Decimal(str(state["reserved_usd"])) != 0:
        raise ValueError(
            "the paper family still holds a broker reservation after a fault"
        )
    return state


def _settle_candidate_processing_fault(
    db: Database,
    *,
    run_id: str,
    family_id: str,
    error: BaseException,
    stage: str,
) -> list[str]:
    """Record the fault on every in-flight call record of one family.

    The row stays ``incomplete_infra``: a call was opened and its outcome is
    unknown. It now also names the exception class, the message and the stage,
    so the skipped family is answerable without the launcher log.
    """
    settled: list[str] = []
    for row in _incomplete_infra_records(db, run_id, family_id):
        try:
            record = json.loads(row["candidate_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        record["reason_code"] = CANDIDATE_PROCESSING_FAULT_REASON_CODE
        record["candidate_processing_fault"] = {
            "contract_version": CANDIDATE_PROCESSING_FAULT_CONTRACT_VERSION,
            "error_class": type(error).__name__,
            "error_message": str(error),
            "stage": stage,
        }
        with db.transaction():
            db.connection.execute(
                """UPDATE candidates SET candidate_json=?,updated_at=?
                WHERE item_id=?""",
                (canonical_json(record), now(), row["item_id"]),
            )
        settled.append(str(row["item_id"]))
    return settled


def _record_candidate_processing_fault(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str | None,
    family_id: str,
    selected: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    """Record one contained paper fault as a generation routing row."""
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,?,'generation_routing',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    "generation_routing",
                    family_id,
                    CANDIDATE_PROCESSING_FAULT_REASON_CODE,
                    detail["error_class"],
                    detail["error_message"],
                    detail["stage"],
                ),
                source_id,
                CANDIDATE_PROCESSING_FAULT_REASON_CODE,
                canonical_json(
                    {
                        "campaign_id": campaign_id,
                        "candidate_key": candidate_key,
                        "family_id": family_id,
                        "selection": selected,
                        **detail,
                    }
                ),
                now(),
            ),
        )


def _contain_candidate_processing_fault(
    db: Database,
    progress: _Progress,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str | None,
    family_id: str,
    selected: dict[str, Any],
    title: str | None,
    provider: Provider,
    error: Exception,
) -> dict[str, Any]:
    """Settle one paper family a fault stopped and let the producer continue.

    The reservation check runs first: a fault that left money in flight is not
    contained, and the original exception ends the producer as before.
    """
    stage = progress.last_error_stage or progress.stage
    try:
        cost_state = _released_family_reservation(provider, family_id)
    except Exception as reservation_error:
        raise reservation_error from error
    settled = _settle_candidate_processing_fault(
        db,
        run_id=campaign_id,
        family_id=family_id,
        error=error,
        stage=stage,
    )
    detail = {
        "contract_version": CANDIDATE_PROCESSING_FAULT_CONTRACT_VERSION,
        "error_class": type(error).__name__,
        "error_message": str(error),
        "stage": stage,
        "source_id": source_id,
        "settled_call_records": settled,
        "family_cost_state": cost_state,
    }
    _record_candidate_processing_fault(
        db,
        campaign_id=campaign_id,
        candidate_key=candidate_key,
        source_id=source_id,
        family_id=family_id,
        selected=selected,
        detail=detail,
    )
    # ``progress.error`` wrote the run state as "error" on its way out. The
    # family is settled, so the producer is running again.
    progress.paper(
        paper_id=source_id or candidate_key,
        title=title,
        current_stage="completed",
        final_state=CANDIDATE_PROCESSING_FAULT_REASON_CODE,
        final_reason=type(error).__name__,
    )
    progress.last_error_stage = None
    return detail


def _record_paper_cost_cap(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str | None,
    family_id: str,
    selected: dict[str, Any],
    error: PaperCostCapError,
    cost_state: dict[str, str] | None,
    attempt: dict[str, Any] | None = None,
) -> None:
    """Record one paper family the per-paper cost cap stopped.

    The cap bounds one family. The row keeps the reason code, the money the
    family already holds and the stage the refusal landed on, so the skipped
    family is answerable later without the ledger.
    """
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "family_id": family_id,
        "error": str(error),
        "selection": selected,
        "paper_cost_cap_reached": True,
        "broker_stage": error.stage or None,
        "family_cost_state": cost_state,
    }
    if attempt is not None:
        detail["generation_attempt"] = attempt
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,?,'paper_cost_cap',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    "paper_cost_cap",
                    family_id,
                    error.stage or "",
                    PAPER_COST_CAP_REASON_CODE,
                ),
                source_id,
                PAPER_COST_CAP_REASON_CODE,
                canonical_json(detail),
                now(),
            ),
        )


def _generation_counts(db: Database, run_id: str) -> dict[str, int]:
    """Return reproducible path, candidate, finding, and settled-call counts."""
    attempt_ids: set[str] = set()
    finding_indexes: set[int] = set()
    revision_attempt_ids: set[str] = set()
    candidate_count = 0
    for row in db.rows(
        f"""SELECT candidate_json FROM candidates WHERE run_id=?
        AND {BENCHMARK_CANDIDATE_PREDICATE}""",
        (run_id,),
    ):
        candidate = json.loads(row["candidate_json"])
        if not _is_current_contract_candidate(candidate):
            continue
        candidate_count += 1
        attempt = (candidate.get("provenance") or {}).get("generation_attempt")
        if isinstance(attempt, dict) and attempt.get("contract_version") == (
            GENERATION_ATTEMPT_CONTRACT_VERSION
        ):
            attempt_ids.add(str(attempt["attempt_id"]))
            finding_indexes.add(int(attempt["finding_attempt_index"]))
            if attempt.get("question_revision_index") in {1, 2}:
                revision_attempt_ids.add(str(attempt["attempt_id"]))
        else:
            family_id = candidate.get("source", {}).get("paper_family_id")
            attempt_ids.add(
                stable_id(
                    "generation-attempt",
                    run_id,
                    family_id,
                    1,
                    0,
                    GENERATION_ATTEMPT_CONTRACT_VERSION,
                )
            )
            finding_indexes.add(1)

    base_policy = generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
    for row in db.rows(
        """SELECT paper_family_id,selection_policy_version FROM findings
        WHERE run_id=?""",
        (run_id,),
    ):
        policy = row["selection_policy_version"]
        if not isinstance(policy, str) or not policy.startswith(
            f"{base_policy}:finding-"
        ):
            continue
        try:
            index = int(policy.rsplit("-", 1)[1])
        except ValueError:
            continue
        if index not in {1, 2}:
            continue
        attempt_ids.add(
            stable_id(
                "generation-attempt",
                run_id,
                row["paper_family_id"],
                index,
                0,
                GENERATION_ATTEMPT_CONTRACT_VERSION,
            )
        )
        finding_indexes.add(index)

    for row in db.rows(
        """SELECT detail_json FROM rejection_ledger
        WHERE stage IN ('generation','generation_budget')"""
    ):
        detail = json.loads(row["detail_json"])
        if detail.get("campaign_id") != run_id:
            continue
        attempt = detail.get("generation_attempt")
        if not isinstance(attempt, dict) or attempt.get("contract_version") != (
            GENERATION_ATTEMPT_CONTRACT_VERSION
        ):
            continue
        attempt_ids.add(str(attempt["attempt_id"]))
        finding_indexes.add(int(attempt["finding_attempt_index"]))
        if attempt.get("question_revision_index") in {1, 2}:
            revision_attempt_ids.add(str(attempt["attempt_id"]))

    settled_call_count = db.one(
        """SELECT COUNT(*) AS count FROM calls
        WHERE run_id=? AND status <> 'started'""",
        (run_id,),
    )["count"]
    return {
        "model_call_count": int(settled_call_count),
        "qa_candidate_count": candidate_count,
        "finding_attempt_count": len(finding_indexes),
        "candidate_path_count": len(attempt_ids),
        "question_revision_count": len(revision_attempt_ids),
    }


def _validation_event_for_stored_candidate(
    db: Database, candidate: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the newest validation result bound to this exact stored payload."""
    candidate_hash = stable_id("candidate-payload", candidate["candidate_json"])
    return db.one(
        """SELECT label,reason_codes_json FROM validation_events
        WHERE item_id=? AND json_extract(details_json, '$.candidate_hash')=?
        ORDER BY created_at DESC,event_id DESC LIMIT 1""",
        (candidate["item_id"], candidate_hash),
    )


def _candidate_for_current_contract(
    db: Database,
    *,
    run_id: str,
    source_id: str,
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return only a candidate made with every current generation contract."""
    rows = db.rows(
        f"""SELECT item_id,status,candidate_json FROM candidates
        WHERE run_id=? AND source_id=?
        AND {BENCHMARK_CANDIDATE_PREDICATE}
        ORDER BY updated_at DESC,item_id DESC""",
        (run_id, source_id),
    )
    for row in rows:
        if statuses is not None and row["status"] not in statuses:
            continue
        candidate = json.loads(row["candidate_json"])
        if _is_current_contract_candidate(candidate):
            return row
    return None


def _bind_provider(
    provider: Provider,
    *,
    paper_id: str,
    family_id: str,
    source_version_id: str,
) -> Provider:
    bind = getattr(provider, "bind", None)
    if bind is None:
        return provider
    return bind(
        paper_id=paper_id,
        family_id=family_id,
        source_version_id=source_version_id,
    )


# One paper can hold three rows under the active versions: the first screening,
# its bounded format re-ask, and its bounded geography re-screen. The last one
# that ran is the paper's answer, so the loader orders them and takes it.
_ELIGIBILITY_ATTEMPT_RANK = {
    "initial": 0,
    "format_repair": 1,
    "geography_rescreen": 2,
}


def _attempt_order(item: dict[str, Any]) -> tuple[int, int]:
    attempt = item.get("eligibility_attempt") or {}
    kind = str(attempt.get("kind") or "initial")
    return (
        _ELIGIBILITY_ATTEMPT_RANK.get(kind, 0),
        int(attempt.get("attempt") or 1),
    )


def _load_eligibility_jobs(
    run_dir: Path,
    *,
    prompt_file: Path | None,
    schema_file: Path | None,
    policy_file: Path | None,
    rescreen_prompt_file: Path | None = None,
) -> dict[str, dict[str, Any]]:
    versioned = (
        prompt_file is not None and schema_file is not None and policy_file is not None
    )
    prompt_hashes = (
        {
            sha256_file(path)
            for path in (prompt_file, rescreen_prompt_file)
            if path is not None
        }
        if versioned
        else set()
    )
    schema_hash = sha256_file(schema_file) if versioned else None
    policy_hash = sha256_file(policy_file) if versioned else None
    selected: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "jobs").glob("*.json")):
        item = _read(path)
        if versioned and (
            item.get("prompt_sha256") not in prompt_hashes
            or item.get("schema_sha256") != schema_hash
            or item.get("policy_sha256") != policy_hash
        ):
            continue
        key = str(item["candidate_key"])
        held = selected.get(key)
        if held is None:
            selected[key] = item
            continue
        if _attempt_order(item) == _attempt_order(held):
            raise ValueError(
                "a candidate has more than one eligibility job for the active version"
            )
        if _attempt_order(item) > _attempt_order(held):
            selected[key] = item
    return selected


def _trusted_brokered_eligibility_decisions(
    *,
    selection: list[dict[str, Any]],
    access_items: dict[str, dict[str, Any]],
    eligibility_jobs: dict[str, dict[str, Any]],
    verifier: Provider,
    max_papers: int,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    rescreen_prompt_file: Path | None = None,
    completions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    decisions: dict[str, str] = {}
    labelled = completions or {}
    processed = 0
    for selected in selection:
        if processed >= max_papers:
            break
        candidate_key = selected.get("candidate_key")
        access = access_items.get(candidate_key)
        if access is None or access.get("access_state") != "full_text_ready":
            continue
        processed += 1
        completion = labelled.get(str(candidate_key))
        if completion is not None:
            # The paper is finished. Its recorded eligibility decision keeps the
            # run counts true without reading one receipt of it again.
            decision = completion["eligibility_decision"]
            if decision is not None:
                decisions[str(candidate_key)] = str(decision)
            continue
        eligibility = eligibility_jobs.get(candidate_key)
        if eligibility is None or eligibility.get("execution_authority") != (
            "shared_gemini_broker"
        ):
            continue
        family_id = str(
            access.get("paper_family_id")
            or stable_id("family", access.get("doi") or candidate_key)
        )
        paper_verifier = _bind_provider(
            verifier,
            paper_id=str(candidate_key),
            family_id=family_id,
            source_version_id=str(access["source_content_hash"]),
        )
        _validate_access_integrity(access, eligibility)
        validation = _validate_brokered_eligibility(
            eligibility,
            paper_verifier,
            access=access,
            prompt_file=prompt_file,
            schema_file=schema_file,
            policy_file=policy_file,
            rescreen_prompt_file=rescreen_prompt_file,
        )
        decision = validation["decision"]
        if validation["valid"] is not True and decision != "uncertain":
            raise ValueError("invalid deterministic eligibility must remain uncertain")
        decisions[str(candidate_key)] = decision
    return decisions


def _accepted_count(db: Database, run_id: str) -> int:
    row = db.one(
        """SELECT COUNT(DISTINCT paper_family_id) AS count FROM candidates
        WHERE run_id=? AND status='machine_accepted_unverified'""",
        (run_id,),
    )
    return int(row["count"])


_AUTHOR_ROLE_PROVIDER = "author"
_ENFORCED_ROLE_PHASES = frozenset({"away_production"})


def _resolve_model_roles(
    *,
    author: Provider,
    verifier: Provider,
    roles_file: Path | None,
    role_profile: str | None,
) -> dict[str, Any]:
    """Load the role contract and bind it to the providers before any call.

    The contract itself is validated on every run, so a collapsed role map can
    never reach a paid call. A named profile additionally binds the providers:
    each role's requested model must equal the configured one, and the writer
    must not share a model with any judge. A production phase must name a
    profile, because that is the run the audit found judging its own output.
    """
    contract = load_role_contract(roles_file)
    phase = str(getattr(author, "phase", "offline"))
    # Chapter 2 yield audit, section 4.9 C9 (cost slice): a production run
    # never selects the cost_aware profile.
    assert_profile_allowed_for_phase(role_profile, phase)
    effective = {
        WRITER_ROLE: provider_model(author, WRITER_ROLE),
        **{role: provider_model(verifier, role) for role in JUDGE_ROLES},
    }
    if role_profile is None:
        if phase in _ENFORCED_ROLE_PHASES:
            raise ValueError(
                "a production streaming run must name a model role profile"
            )
        return {
            "contract_version": MODEL_ROLES_CONTRACT_VERSION,
            "profile": None,
            "enforced": False,
            "resolved_roles": None,
            "effective_role_models": effective,
            "same_model_roles": role_separation_error(effective) is not None,
        }
    resolved = resolve_roles(contract, role_profile)
    mismatched = sorted(
        role
        for role, assignment in resolved.items()
        if role in effective and effective[role] != assignment["model"]
    )
    if mismatched:
        raise ValueError(
            "the streaming providers do not serve the configured model roles: "
            + ", ".join(mismatched)
        )
    assert_role_separation(effective)
    return {
        "contract_version": MODEL_ROLES_CONTRACT_VERSION,
        "profile": role_profile,
        "enforced": True,
        "resolved_roles": resolved,
        "effective_role_models": effective,
        "same_model_roles": False,
    }


def _write_run_manifest(
    namespace: Path,
    *,
    run_id: str,
    campaign_id: str,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    access_manifest: dict[str, Any],
    author: Provider,
    verifier: Provider,
    eligibility_prompt_file: Path | None,
    eligibility_schema_file: Path | None,
    eligibility_policy_file: Path | None,
    eligibility_rescreen_prompt_file: Path | None,
    model_roles: dict[str, Any],
) -> Path:
    def file_hash(path: Path | None) -> str | None:
        return sha256_file(path) if path is not None and path.is_file() else None

    manifest = {
        "schema": "streaming-dataset-run-manifest-v1",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "phase": getattr(author, "phase", "offline"),
        "access_run_dir": str(access_run_dir.resolve()),
        "access_manifest_sha256": sha256_file(access_run_dir / "run-manifest.json"),
        "access_completion_receipt_sha256": sha256_file(
            access_run_dir / "run-receipt.json"
        ),
        "selection_sha256": sha256_bytes(
            canonical_json(access_manifest["selection"]).encode()
        ),
        "eligibility_run_dir": str(eligibility_run_dir.resolve()),
        "eligibility_prompt_sha256": file_hash(eligibility_prompt_file),
        "eligibility_schema_sha256": file_hash(eligibility_schema_file),
        "eligibility_policy_sha256": file_hash(eligibility_policy_file),
        "eligibility_rescreen_prompt_sha256": file_hash(
            eligibility_rescreen_prompt_file
        ),
        "author": {"provider": author.name, "model": author.model},
        "verifier": {"provider": verifier.name, "model": verifier.model},
        "model_roles": model_roles,
        "generation_arm": "answer_first",
        "export_seed": "streaming-20260912",
    }
    path = (
        namespace
        / "streaming-dataset-r1"
        / "runs"
        / stable_id("stream-invocation", run_id)
        / "run-manifest.json"
    )
    try:
        atomic_json(path, manifest, immutable=True)
    except FileExistsError as error:
        raise ValueError("the immutable streaming run inputs changed") from error
    return path


class _Progress:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        invocation_run_id: str,
        run_manifest_file: Path,
        counts: dict[str, int],
    ) -> None:
        self.path = path.resolve()
        self.run_id = run_id
        self.invocation_run_id = invocation_run_id
        self.run_manifest_file = run_manifest_file.resolve()
        self.counts = counts
        self.recent: list[dict[str, Any]] = []
        self.state = "running"
        self.stage = "eligibility"
        self.message = "Streaming pipeline started."
        self.broker_status_file: Path | None = None
        self.budget_policy_file: Path | None = None
        self.dataset_metadata_file: Path | None = None
        # The stage the last recorded error landed on. The containment path
        # reads it to name the stage on a settled paper fault.
        self.last_error_stage: str | None = None

    def write(self, state: str, stage: str, message: str) -> None:
        self.state = state
        self.stage = stage
        self.message = message
        custody: dict[str, str] = {
            "run_manifest_sha256": sha256_file(self.run_manifest_file)
        }
        if self.broker_status_file is not None:
            custody["broker_status_sha256"] = sha256_file(self.broker_status_file)
            custody["budget_policy_sha256"] = sha256_file(self.budget_policy_file)
        if self.dataset_metadata_file is not None:
            custody["dataset_metadata_sha256"] = sha256_file(self.dataset_metadata_file)
        atomic_json(
            self.path,
            {
                "schema": "streaming-dataset-progress-v1",
                "state": state,
                "run_id": self.run_id,
                "invocation_run_id": self.invocation_run_id,
                "current_stage": stage,
                "updated_at_utc": now(),
                "message": message,
                "counts": self.counts,
                "recent_papers": self.recent[-100:],
                **custody,
            },
        )

    def attach_broker(self, broker: Any) -> None:
        self.broker_status_file = broker.ledger_file.with_name(
            f"{broker.ledger_file.stem}.status.json"
        )
        self.budget_policy_file = broker.policy_file
        broker.set_status_observer(self._broker_state_changed)

    def _broker_state_changed(self, status_file: Path) -> None:
        if status_file != self.broker_status_file:
            raise ValueError("the broker status observer received another ledger")
        self.write(self.state, self.stage, self.message)

    def set_dataset_metadata(self, path: Path) -> None:
        metadata = _read(path)
        if metadata.get("run_id") != self.run_id:
            raise ValueError("the dataset metadata and progress run IDs do not match")
        self.dataset_metadata_file = path.resolve()
        self.write(self.state, self.stage, self.message)

    def increment(self, name: str) -> None:
        self.counts[name] = int(self.counts.get(name, 0)) + 1

    def set_count(self, name: str, value: int) -> None:
        self.counts[name] = value

    def paper(
        self,
        *,
        paper_id: str,
        title: str | None,
        current_stage: str,
        final_state: str | None = None,
        final_reason: str | None = None,
    ) -> None:
        row = {
            "paper_id": paper_id,
            "title": title,
            "current_stage": current_stage,
            "final_state": final_state,
            "final_reason": final_reason,
        }
        self.recent = [
            old for old in self.recent if old.get("paper_id") != paper_id
        ] + [row]
        self.write("running", current_stage, f"Processing {paper_id}.")

    def error(
        self,
        paper_id: str,
        title: str | None,
        stage: str,
        error: Exception,
    ) -> None:
        self.last_error_stage = stage
        self.paper(
            paper_id=paper_id,
            title=title,
            current_stage=stage,
            final_state="error",
            final_reason=type(error).__name__,
        )
        self.write("error", stage, f"Streaming stopped on {type(error).__name__}.")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected one JSON object: {path}")
    return value


def _eligibility_job_key(
    access: dict[str, Any],
    config: dict[str, Any],
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    provider: Provider,
    *,
    attempt_note: dict[str, Any] | None = None,
) -> str:
    """Bind one eligibility identity to the exact bytes that were sent.

    A bounded re-ask and a bounded geography re-screen send a different payload,
    so each one gets its own key, its own broker receipt and its own job row. The
    note is the same object the payload carries, so the identity is rebuildable
    from the stored row alone.
    """
    identity: dict[str, Any] = {
        "base_job_key": _job_key(access, config, prompt_file, schema_file, policy_file),
        "provider_identity": provider.request_identity(),
        "model": provider.model,
        "authority": "shared_gemini_broker",
    }
    if attempt_note is not None:
        identity["attempt_note"] = attempt_note
    return sha256_bytes(canonical_json(identity).encode())


def _eligibility_attempt(
    db: Database,
    access: dict[str, Any],
    run_dir: Path,
    *,
    run_id: str,
    provider: Provider,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    text: str,
    kind: str,
    attempt: int,
    attempt_note: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make one eligibility call and write its receipt-bound job row."""
    broker = provider.broker
    config = broker.config
    schema = _read(schema_file)
    policy = _read(policy_file)
    job_key = _eligibility_job_key(
        access,
        config,
        prompt_file,
        schema_file,
        policy_file,
        provider,
        attempt_note=attempt_note,
    )
    request, hashes = _request_payload(
        source=access,
        policy=policy,
        text=text,
        prompt=prompt_file.read_text(encoding="utf-8"),
        schema=schema,
        config=config,
        request_id=job_key,
        policy_sha256=sha256_file(policy_file),
        repair=attempt_note,
    )
    span_manifest_reference = _persist_span_manifest_v2(
        run_dir,
        job_key,
        text,
        str(access["extraction_sha256"]),
        schema,
    )
    generation = request["generationConfig"]
    parameters = {
        "temperature": generation.get("temperature", 0),
        "max_tokens": generation["maxOutputTokens"],
        "json_schema": schema,
    }
    result = call_provider(
        db,
        provider,
        run_id=run_id,
        entity_id=stable_id("eligibility", access["candidate_key"], job_key),
        role="eligibility",
        system=request["systemInstruction"]["parts"][0]["text"],
        prompt=request["contents"][0]["parts"][0]["text"],
        prompt_version=prompt_file.stem,
        parameters=parameters,
        response_schema=schema,
        reservation=Decimal("0"),
        timeout=call_timeout_seconds(config, "eligibility"),
        retries=0,
        rate_limit_seconds=0,
    )
    validation = validate_response(
        result.payload,
        _validation_evidence(text, str(access["extraction_sha256"]), schema),
        expected={
            "request_id": job_key,
            "input_echo": hashes,
            "correction_metadata": _correction_metadata(access),
            "known_context_gaps": _known_context_gaps(access),
        },
        response_schema=schema,
        frozen_criterion_statuses=_frozen_rescreen_statuses(attempt_note),
    )
    identity = provider.request_identity()
    request_key = broker_request_key(
        model=provider.model,
        run_id=provider.invocation_run_id,
        phase=provider.phase,
        stage="eligibility",
        paper_id=identity["paper_id"],
        family_id=identity["family_id"],
        source_version_id=identity["source_version_id"],
        payload=request,
    )
    job = {
        "schema": "gemini-eligibility-job-v1",
        "job_key": job_key,
        "execution_authority": "shared_gemini_broker",
        "broker_request_key": request_key,
        "broker_receipt_sha256": sha256_file(
            broker.effective_receipt_path(request_key)
        ),
        "candidate_key": access["candidate_key"],
        "model": provider.model,
        "model_version": result.returned_model,
        "response_id": result.request_id,
        "state": "completed" if validation["valid"] else "screening_error",
        "source_content_hash": access["source_content_hash"],
        "extraction_sha256": access["extraction_sha256"],
        "policy_sha256": sha256_file(policy_file),
        "prompt_sha256": sha256_file(prompt_file),
        "schema_sha256": sha256_file(schema_file),
        "eligibility_attempt": {
            "kind": kind,
            "attempt": attempt,
            "attempt_note": attempt_note,
        },
        **span_manifest_reference,
        "parsed_response": result.payload,
        "validation": validation,
        "shadow_two_pass": shadow_two_pass_measurement(
            text, validation.get("resolved_eligible_arctic_scope")
        ),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "actual_cost_usd": (
            str(result.actual_cost_usd) if result.actual_cost_usd is not None else None
        ),
        "completed_at_utc": now(),
    }
    atomic_json(run_dir / "jobs" / f"{job_key}.json", job, immutable=True)
    return job


# A refused repair leaves the first screening's science untouched. The paper
# spent its bounded attempts on the shape of the answer, so it stays
# re-screenable under a later prompt or schema version, exactly as the batch
# path records it (audit 4.7, finding E6).
_REPAIR_REFUSAL_CODES = frozenset({"repair_changed_criterion_status"})


def _eligibility_is_rescreenable(errors: Any) -> bool:
    return format_repairable(errors) or (
        isinstance(errors, list)
        and bool(errors)
        and set(errors) <= _REPAIR_REFUSAL_CODES
    )


def _brokered_eligibility_state(
    eligibility: dict[str, Any], validation: dict[str, Any]
) -> str:
    """Keep a formatting mistake re-screenable after the re-validation.

    A paper that spent its bounded re-ask attempts on a formatting mistake is not
    a screening error, and a later prompt or schema version screens it again
    (audit 4.7, finding E6).
    """
    if validation["valid"]:
        return "completed"
    if eligibility.get("state") == UNRESOLVED_STATE and _eligibility_is_rescreenable(
        validation.get("errors")
    ):
        return UNRESOLVED_STATE
    return "screening_error"


def _criterion_statuses(job: dict[str, Any]) -> dict[str, Any]:
    return {
        str(row.get("criterion_id")): row.get("status")
        for row in (job.get("parsed_response") or {}).get("criteria", [])
        if isinstance(row, dict)
    }


def _frozen_rescreen_statuses(
    attempt_note: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the statuses a geography re-screen must keep, or None.

    A re-screen decides one criterion, but the response schema needs all five
    rows. The four frozen rows are read from this note, never from the answer.
    """
    if not isinstance(attempt_note, dict):
        return None
    if attempt_note.get("kind") != "geography_rescreen":
        return None
    frozen = attempt_note.get("frozen_criterion_statuses")
    return frozen if isinstance(frozen, dict) else None


def _rescreen_note(job: dict[str, Any]) -> dict[str, Any]:
    """Freeze the four criteria the geography re-screen may not touch."""
    return {
        "kind": "geography_rescreen",
        "frozen_criterion_statuses": {
            name: status
            for name, status in _criterion_statuses(job).items()
            if name != "study_geography"
        },
        "instruction": (
            "A first screening left study_geography unresolved. Decide that one "
            "criterion again from the supplied spans. Every other criterion keeps "
            "the status in frozen_criterion_statuses. Do not change any of them."
        ),
    }


def _rescreen_moved_a_frozen_status(note: dict[str, Any], job: dict[str, Any]) -> bool:
    """Say whether a re-screen answer moved a criterion it had to keep."""
    frozen = note.get("frozen_criterion_statuses") or {}
    current = _criterion_statuses(job)
    return any(current.get(name) != status for name, status in frozen.items())


def _run_eligibility(
    db: Database,
    access: dict[str, Any],
    run_dir: Path,
    *,
    run_id: str,
    provider: Provider,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    rescreen_prompt_file: Path | None = None,
) -> dict[str, Any]:
    """Screen one paper, with the bounded re-ask and the bounded re-screen.

    Chapter 2 lost 38 of 200 papers here, because the re-ask lived only in the
    batch module and the re-screen selected nothing (audit 4.7, findings E2 and
    E6). Both now run inside the same pass, each bounded by its own attempt
    count, and neither can move a criterion status.
    """
    if getattr(provider, "broker", None) is None:
        raise ValueError("new eligibility calls require the shared broker")
    text = Path(access["extraction_path"]).read_text(encoding="utf-8")
    job = _eligibility_attempt(
        db,
        access,
        run_dir,
        run_id=run_id,
        provider=provider,
        prompt_file=prompt_file,
        schema_file=schema_file,
        policy_file=policy_file,
        text=text,
        kind="initial",
        attempt=1,
    )
    # One bounded format re-ask. A formatting mistake is a mistake about how the
    # answer is written, never about the science, so the paper stays re-screenable
    # instead of ending as a screening error.
    attempt = 1
    while (
        not job["validation"]["valid"]
        and format_repairable(job["validation"]["errors"])
        and attempt < MAXIMUM_FORMAT_ATTEMPTS
    ):
        prior = {
            "attempts": attempt,
            "format_errors": list(job["validation"]["errors"]),
            "parsed_response": job["parsed_response"],
            "validation": job["validation"],
        }
        attempt += 1
        repaired = _eligibility_attempt(
            db,
            access,
            run_dir,
            run_id=run_id,
            provider=provider,
            prompt_file=prompt_file,
            schema_file=schema_file,
            policy_file=policy_file,
            text=text,
            kind="format_repair",
            attempt=attempt,
            attempt_note=_repair_note(prior),
        )
        refusal = None
        if repaired["validation"]["valid"]:
            if _repair_moved_a_status(prior, repaired["parsed_response"]):
                # A repair corrects the shape of an answer. A repair that moves a
                # criterion status is a new scientific judgment, so refuse it.
                refusal = "repair_changed_criterion_status"
            elif not repaired_phrases_are_specific(repaired["parsed_response"]):
                # A repaired phrase must still name a station, region, stratum or
                # population. A bound phrase that identifies nothing is not a fix.
                refusal = "eligible_arctic_scope_phrase_not_specific"
        if refusal is not None:
            repaired = {
                **repaired,
                "state": "screening_error",
                "validation": {
                    **repaired["validation"],
                    "valid": False,
                    "errors": [refusal],
                    "decision": "uncertain",
                },
            }
        job = repaired
    if not job["validation"]["valid"] and _eligibility_is_rescreenable(
        job["validation"]["errors"]
    ):
        # The paper spent its bounded attempts on the shape of its answer. It is
        # not a screening error, and a later prompt or schema version screens it
        # again.
        job = {**job, "state": UNRESOLVED_STATE}
    if job["validation"]["valid"] and geography_rescreen_eligible(
        _criterion_statuses(job)
    ):
        job = _run_geography_rescreen(
            db,
            access,
            run_dir,
            run_id=run_id,
            provider=provider,
            prior=job,
            text=text,
            rescreen_prompt_file=rescreen_prompt_file,
            schema_file=schema_file,
            policy_file=policy_file,
        )
    return job


def _run_geography_rescreen(
    db: Database,
    access: dict[str, Any],
    run_dir: Path,
    *,
    run_id: str,
    provider: Provider,
    prior: dict[str, Any],
    text: str,
    rescreen_prompt_file: Path | None,
    schema_file: Path,
    policy_file: Path,
) -> dict[str, Any]:
    """Re-decide study_geography once, with the other four criteria frozen.

    The re-screen reads the same spans under the same ordered geography
    procedure, with the v8 span and phrase blocks, so a recovered paper enters
    with the same phrase discipline. It decides one criterion. A failed geography
    is a decision and is never re-screened, so a correct exclusion never returns.
    """
    if rescreen_prompt_file is None:
        return {**prior, "geography_rescreen": "skipped_no_prompt"}
    note = _rescreen_note(prior)
    rescreened = _eligibility_attempt(
        db,
        access,
        run_dir,
        run_id=run_id,
        provider=provider,
        prompt_file=rescreen_prompt_file,
        schema_file=schema_file,
        policy_file=policy_file,
        text=text,
        kind="geography_rescreen",
        attempt=1,
        attempt_note=note,
    )
    if not rescreened["validation"]["valid"]:
        return {**prior, "geography_rescreen": "unresolved_invalid_response"}
    if _rescreen_moved_a_frozen_status(note, rescreened):
        # The re-screen decides one criterion. An answer that moves another is a
        # second scientific opinion the run did not ask for, so refuse it.
        return {**prior, "geography_rescreen": "refused_moved_frozen_status"}
    return {
        **rescreened,
        "geography_rescreen": "applied",
        "rescreened_from": prior["job_key"],
    }


def _validate_brokered_eligibility(
    eligibility: dict[str, Any],
    provider: Provider,
    *,
    access: dict[str, Any],
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    rescreen_prompt_file: Path | None = None,
) -> dict[str, Any]:
    if eligibility.get("execution_authority") != "shared_gemini_broker":
        raise ValueError("the eligibility job did not use the shared broker")
    broker = provider.broker
    # A bounded re-ask and a bounded re-screen record the exact note they sent,
    # so the stored row rebuilds its own request and stays receipt-bound.
    attempt = eligibility.get("eligibility_attempt") or {}
    attempt_note = attempt.get("attempt_note")
    if attempt.get("kind") == "geography_rescreen":
        if rescreen_prompt_file is None:
            raise ValueError("a re-screened eligibility job needs its re-screen prompt")
        prompt_file = rescreen_prompt_file
    schema = _read(schema_file)
    policy = _read(policy_file)
    expected_job_key = _eligibility_job_key(
        access,
        broker.config,
        prompt_file,
        schema_file,
        policy_file,
        provider,
        attempt_note=attempt_note,
    )
    text = Path(access["extraction_path"]).read_text(encoding="utf-8")
    request, hashes = _request_payload(
        source=access,
        policy=policy,
        text=text,
        prompt=prompt_file.read_text(encoding="utf-8"),
        schema=schema,
        config=broker.config,
        request_id=expected_job_key,
        policy_sha256=sha256_file(policy_file),
        repair=attempt_note,
    )
    response_version = (
        schema.get("properties", {}).get("schema_version", {}).get("const")
    )
    if response_version in SPAN_CONTRACT_VERSIONS:
        manifest_path = Path(str(eligibility.get("span_manifest_path") or ""))
        expected_manifest = _span_manifest_v2(
            _span_blocks_v2(text, str(access["extraction_sha256"])),
            response_version,
        )
        expected_manifest_sha256 = sha256_bytes(
            canonical_json(expected_manifest).encode()
        )
        if (
            manifest_path.name != f"{expected_job_key}.json"
            or manifest_path.parent.name != "span-manifests"
            or not manifest_path.is_file()
            or _read(manifest_path) != expected_manifest
            or eligibility.get("span_manifest_sha256") != expected_manifest_sha256
            or hashes.get("span_manifest_sha256") != expected_manifest_sha256
        ):
            raise ValueError("the eligibility span manifest does not match")
    identity = provider.request_identity()
    expected_request_key = broker_request_key(
        model=provider.model,
        run_id=provider.invocation_run_id,
        phase=provider.phase,
        stage="eligibility",
        paper_id=identity["paper_id"],
        family_id=identity["family_id"],
        source_version_id=identity["source_version_id"],
        payload=request,
    )
    request_key = eligibility.get("broker_request_key")
    if (
        eligibility.get("job_key") != expected_job_key
        or request_key != expected_request_key
        or eligibility.get("model") != provider.model
        or eligibility.get("policy_sha256") != sha256_file(policy_file)
        or eligibility.get("prompt_sha256") != sha256_file(prompt_file)
        or eligibility.get("schema_sha256") != sha256_file(schema_file)
    ):
        raise ValueError("the brokered eligibility request identity does not match")
    if not isinstance(request_key, str):
        raise ValueError("the brokered eligibility request key is missing")
    receipt, result = provider.read_receipt(
        request_key=request_key,
        role="eligibility",
        request_sha256=sha256_bytes(canonical_json(request).encode()),
    )
    receipt_path = broker.effective_receipt_path(request_key)
    if (
        eligibility.get("broker_receipt_sha256") != sha256_file(receipt_path)
        or receipt.get("state") != "completed"
        or result.payload != eligibility.get("parsed_response")
        or result.returned_model != eligibility.get("model_version")
        or result.request_id != eligibility.get("response_id")
    ):
        raise ValueError("the brokered eligibility job and receipt do not match")
    validation = validate_response(
        result.payload,
        _validation_evidence(text, str(access["extraction_sha256"]), schema),
        expected={
            "request_id": expected_job_key,
            "input_echo": hashes,
            "correction_metadata": _correction_metadata(access),
            "known_context_gaps": _known_context_gaps(access),
        },
        response_schema=schema,
        frozen_criterion_statuses=_frozen_rescreen_statuses(attempt_note),
    )
    return validation


def _validate_access_integrity(
    access: dict[str, Any], eligibility: dict[str, Any]
) -> None:
    if (
        access.get("schema") != "article-access-item-v1"
        or access.get("access_state") != "full_text_ready"
        or access.get("identity_verified") is not True
    ):
        raise ValueError("the article-access item is not full-text ready")
    if eligibility.get("schema") != "gemini-eligibility-job-v1":
        raise ValueError("the Gemini eligibility receipt schema is invalid")
    if (
        access.get("candidate_key") != eligibility.get("candidate_key")
        or access.get("source_content_hash") != eligibility.get("source_content_hash")
        or access.get("extraction_sha256") != eligibility.get("extraction_sha256")
    ):
        raise ValueError("the access and eligibility receipts do not match")
    source_path = Path(str(access.get("source_path") or ""))
    extraction_path = Path(str(access.get("extraction_path") or ""))
    if not source_path.is_file() or sha256_file(source_path) != access.get(
        "source_content_hash"
    ):
        raise ValueError("the ready source object is missing or changed")
    if not extraction_path.is_file() or sha256_file(extraction_path) != access.get(
        "extraction_sha256"
    ):
        raise ValueError("the ready extraction is missing or changed")


def _validate_pair(access: dict[str, Any], eligibility: dict[str, Any]) -> None:
    _validate_access_integrity(access, eligibility)
    if (
        eligibility.get("state") != "completed"
        or (eligibility.get("validation") or {}).get("valid") is not True
    ):
        raise ValueError("the Gemini eligibility receipt is not valid and complete")
    decision = (eligibility.get("validation") or {}).get("decision")
    parsed = eligibility.get("parsed_response") or {}
    version = parsed.get("schema_version")
    decision_consistent = (
        version == "eligibility-response-v1" and parsed.get("overall") == decision
    ) or (
        version in SPAN_CONTRACT_VERSIONS
        and "overall" not in parsed
        and "overall_reason_codes" not in parsed
        and (eligibility.get("validation") or {}).get("mapping_version")
        == ELIGIBILITY_STATUS_MAPPING_VERSION
    )
    if decision not in {"eligible", "excluded", "uncertain"} or not decision_consistent:
        raise ValueError("the Gemini eligibility decision is inconsistent")
    criteria = parsed.get("criteria")
    if (
        parsed.get("request_id") != eligibility.get("job_key")
        or not isinstance(criteria, list)
        or {row.get("criterion_id") for row in criteria if isinstance(row, dict)}
        != {
            "published_primary_findings",
            "stable_identity_version",
            "study_geography",
            "access_rights_evidence",
            "correction_retraction_coverage",
        }
    ):
        raise ValueError("the Gemini eligibility evidence shape is invalid")
    geography = next(
        (
            row
            for row in criteria
            if isinstance(row, dict) and row.get("criterion_id") == "study_geography"
        ),
        {},
    )
    if decision == "eligible" and (
        geography.get("status") != "satisfied" or not geography.get("evidence")
    ):
        raise ValueError("eligible geography lacks located evidence")
    echo = parsed.get("input_echo") or {}
    if echo.get("source_version_sha256") != access.get(
        "source_content_hash"
    ) or echo.get("extracted_text_sha256") != access.get("extraction_sha256"):
        raise ValueError("the Gemini eligibility input hashes do not match")


def _import_source(
    db: Database,
    namespace: Path,
    access: dict[str, Any],
    selected: dict[str, Any],
    eligibility: dict[str, Any],
    *,
    family_id: str,
) -> str:
    source_path = Path(access["source_path"])
    metadata = {
        "stable_id": access["candidate_key"],
        "doi": access.get("doi"),
        "title": access["title"],
        "authors": access.get("authors") or selected.get("authors") or [],
        "published_date": access.get("published_date")
        or selected.get("published_date"),
        "year": access.get("year") or selected.get("year"),
        "discipline": access.get("discipline")
        or selected.get("discipline")
        or "unclassified",
        "source_version": access.get("source_version") or access["source_content_hash"],
        "retrieval_url": access.get("final_url"),
        "license": access.get("license"),
        "paper_family_id": family_id,
        "query": "streaming eligibility bridge",
        "selection": selected,
        "eligibility_job_key": eligibility["job_key"],
        "eligibility_model": eligibility.get("model"),
    }
    source = manual_record(metadata, "streaming_bridge")
    db.upsert_source(source)
    existing = db.one(
        """SELECT relative_path FROM artifacts
        WHERE source_id=? AND kind='original' AND content_hash=?""",
        (source["source_id"], access["source_content_hash"]),
    )
    if existing:
        if (
            sha256_file(namespace / existing["relative_path"])
            != access["source_content_hash"]
        ):
            raise ValueError("the stored source object changed")
    else:
        stored = store_original(
            db,
            namespace,
            source["source_id"],
            source_path.read_bytes(),
            access["media_type"],
            access["final_url"],
        )
        if stored["sha256"] != access["source_content_hash"]:
            raise ValueError(
                "the imported source hash does not match the access receipt"
            )
    extract_source(db, namespace, source["source_id"])
    parsed = eligibility["parsed_response"]
    geography = next(
        row for row in parsed["criteria"] if row["criterion_id"] == "study_geography"
    )
    scope_evidence = {
        "verification_label": "model_reviewed_unverified",
        "eligibility_job_key": eligibility["job_key"],
        "eligibility_model": eligibility.get("model"),
        "returned_model": eligibility.get("model_version"),
        "response_id": eligibility.get("response_id"),
        "decision": eligibility["validation"]["decision"],
        "overall_reason_codes": eligibility["validation"].get(
            "overall_reason_codes", parsed.get("overall_reason_codes", [])
        ),
        **(
            {"status_mapping_version": eligibility["validation"].get("mapping_version")}
            if parsed.get("schema_version") in SPAN_CONTRACT_VERSIONS
            else {}
        ),
        "study_geography": geography,
        "resolved_evidence": [
            row
            for row in eligibility["validation"].get("resolved_evidence", [])
            if row.get("criterion") == "study_geography"
        ],
        **(
            {
                "eligible_arctic_scope": parsed.get("eligible_arctic_scope"),
                "resolved_eligible_arctic_scope": eligibility["validation"].get(
                    "resolved_eligible_arctic_scope"
                ),
            }
            if parsed.get("schema_version") in SCOPE_CONTRACT_VERSIONS
            else {}
        ),
        "known_missing_context": parsed.get("known_missing_context", []),
        "correction_metadata_used": parsed.get("correction_metadata_used"),
        "input_echo": parsed.get("input_echo"),
    }
    with db.transaction():
        db.connection.execute(
            """UPDATE sources
            SET eligibility_state='eligible',geography_state='core_arctic',
                geography_confidence='model_reviewed_unverified',
                inclusion_reason='gemini_full_text_eligibility_with_located_evidence',
                scope_rule_version=?,
                scope_evidence_json=?,updated_at=?
            WHERE source_id=?""",
            (
                "gemini-fulltext-arctic-eligibility-v2"
                if parsed.get("schema_version") in SCOPE_CONTRACT_VERSIONS
                else "gemini-fulltext-arctic-eligibility-v1",
                canonical_json(scope_evidence),
                now(),
                source["source_id"],
            ),
        )
    return source["source_id"]
