from __future__ import annotations

import atexit
import contextlib
import fcntl
import json
import os
import random
import re
import stat
import sys
import threading
import time
import urllib.error
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import (
    BrokerOperationBusyError,
    DuplicateRequestKeyError,
    PaperBindingConflictError,
)
from .gemini_eligibility import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    MAXIMUM_CALL_TIMEOUT_SECONDS,
    GeminiTransport,
    _config,
    _cost,
    _decimal,
    call_timeout_seconds,
    model_config_for_stage,
)
from . import ledger_store
from .ledger_store import LedgerStore
from .pipeline_trace import record_model_request_trace
from .util import atomic_json, canonical_json, sha256_bytes, sha256_file


STAGES = {
    "eligibility",
    "finding_answer_extraction",
    "question_generation",
    "standalone_verification",
    "blinded_reconstruction",
    "answer_agreement",
    "answer_verification",
    "distractor_generation",
    "option_verification",
    "repair",
}
CONSTRUCTION_PHASES = {"live_test", "away_production"}
# Abstention benchmark evaluation (captain order 2026-09-16). Evaluation calls
# share this one ledger and the lifetime ceiling, but they run under their own
# phase, stage family, budget policy, price config and execution gate, so
# evaluation spend never mixes with construction accounting.
EVALUATION_PHASE = "benchmark_evaluation"
PHASES = CONSTRUCTION_PHASES | {EVALUATION_PHASE}
EVALUATION_STAGE_PREFIX = "evaluation_answer:"
EVALUATION_STAGE_PATTERN = re.compile(r"^evaluation_answer:[a-z0-9.-]+$")
EVALUATION_POLICY_SCHEMA = "benchmark-evaluation-policy-v1"
EVALUATION_PRICE_CONFIG_SCHEMA = "benchmark-evaluation-price-config-v1"
EVALUATION_GATE_SCHEMA = "benchmark-evaluation-execution-gate-v1"
EVALUATION_CEILING_REASON = "the paid request exceeds the evaluation ceiling"
# The per-item repeat limit is item-scoped: it says nothing about the next
# item, so a consumer must not treat it as a vendor-wide stop.
EVALUATION_ITEM_REPEAT_REASON = "the evaluation repeat limit for this item is complete"
# An ambiguous evaluation charge halts the evaluation phase only. Construction
# keeps running under its own ceiling (firstmate instruction 2026-09-16: an
# evaluation error must never stop the production pipeline). These two optional
# ledger fields carry that halt; a ledger written before this change has
# neither, which reads as "not halted".
EVALUATION_HALT_FIELDS = ("evaluation_halted", "evaluation_halt_reason")
AMBIGUOUS_HALT_REASON = "ambiguous_generation_charge"
# The ambiguous-charge errors that one reviewed usage reconciliation can
# settle from the saved response, without a provider call.
RECONCILABLE_USAGE_ERRORS = (
    "KeyError: 'thoughtsTokenCount'",
    "ValueError: provider usage is inconsistent",
)
# The evaluation ceiling is a money control, so a larger one needs a reviewed
# transition, chained on the applied predecessor, exactly as a construction
# ceiling does. The baseline is the ceiling the first two evaluation policies
# shared; a policy that changes only the pace or the concurrency needs no
# transition, because it moves no money.
EVALUATION_POLICY_TRANSITION_SCHEMA = "benchmark-evaluation-policy-transition-v1"
EVALUATION_POLICY_TRANSITION_EVENT_SCHEMA = (
    "benchmark-evaluation-policy-transition-event-v1"
)
EVALUATION_BASELINE_CEILING_USD = Decimal("5.00")
# Captain allocation 2026-09-16: USD 200 for benchmarking the Gemini models.
EVALUATION_CEILING_CHANGES = (
    {"evaluation_ceiling_usd": {"from": "5.00", "to": "200.00"}},
)
EVALUATION_POLICY_TRANSITION_FIELDS = {
    "schema",
    "ledger_file",
    "from_evaluation_transition_sha256",
    "from_policy_file",
    "from_policy_sha256",
    "to_policy_sha256",
    "changed_policy_fields",
    "expected_ledger_sha256",
    "evaluation_gate_sha256",
    "integrated_code_commit",
    "review_record",
    "review_record_sha256",
    "reason",
    "authorized_at_utc",
}
EVALUATION_TRANSITION_CHANGED_REASON = "an applied evaluation policy transition changed"
EVALUATION_GATE_BINDING_FIELDS = {
    "eval_set_id",
    "eval_set_manifest_sha256",
    "prompt_version",
    "prompt_sha256",
    "abstention_option_text",
    "models",
    "arms",
    "decoding",
    "repeats_maximum",
    "authorized_run_id",
    "evaluation_policy_sha256",
    "evaluation_price_config_sha256",
}
CONFIG_TRANSITION_V1_FIELDS = {
    "schema",
    "ledger_file",
    "from_price_config_sha256",
    "to_price_config_sha256",
    "expected_ledger_sha256",
    "expected_identity_sha256",
    "execution_gate_sha256",
    "integrated_code_commit",
    "review_record",
    "review_record_sha256",
    "reason",
    "authorized_at_utc",
}
CONFIG_TRANSITION_V2_FIELDS = CONFIG_TRANSITION_V1_FIELDS | {
    "from_config_transition_sha256",
    "from_policy_file",
    "from_policy_sha256",
    "to_policy_sha256",
    "changed_policy_fields",
    "maximum_authorized_cumulative_tranche_usd",
}
CONFIG_TRANSITION_V3_FIELDS = CONFIG_TRANSITION_V1_FIELDS | {
    "from_config_transition_sha256",
    "from_policy_file",
    "from_policy_sha256",
    "to_policy_sha256",
}
UNBOUNDED_COUNT_CHANGE = {
    "live_test_maximum_papers": {"from": 41, "to": None},
    "live_test_maximum_generation_submissions": {"from": 101, "to": None},
}
LIVE_TEST_BUDGET_EXTENSION_CHANGE = {
    "live_test_suballocation_usd": {"from": "10.00", "to": "20.00"}
}
PRODUCTION_BUDGET_EXTENSION_CHANGE = {
    "away_session_total_ceiling_usd": {"from": "25.00", "to": "61.614496"}
}
# Chapter 2 (captain order 2026-09-15): one USD 75.00 allocation on top of the
# USD 33.994972 spent before chapter 2. Halt at exhaustion, no reset, no replay.
CHAPTER2_BUDGET_EXTENSION_CHANGE = {
    "away_session_total_ceiling_usd": {"from": "61.614496", "to": "108.994972"}
}
CHAPTER2_CUMULATIVE_CEILING_USD = Decimal("108.994972")
# Chapter 3 (captain order 2026-09-16): one USD 20.00 allocation on top of the
# USD 53.990121 of construction spend settled when chapter 2 was paused. The
# unspent chapter 2 headroom is retired, so the ceiling moves down to the new
# baseline plus the allocation. Halt at exhaustion, no reset, no replay.
CHAPTER3_CONSTRUCTION_SPEND_BEFORE_USD = Decimal("53.990121")
CHAPTER3_ALLOCATION_USD = Decimal("20.00")
CHAPTER3_CUMULATIVE_CEILING_USD = (
    CHAPTER3_CONSTRUCTION_SPEND_BEFORE_USD + CHAPTER3_ALLOCATION_USD
)
CHAPTER3_BUDGET_CHANGE = {
    "away_session_total_ceiling_usd": {
        "from": str(CHAPTER2_CUMULATIVE_CEILING_USD),
        "to": str(CHAPTER3_CUMULATIVE_CEILING_USD),
    }
}
# Chapter 3 expansion (captain order 2026-09-16 10:27 UTC): the chapter 3
# allocation becomes USD 200.00 in total, so the ceiling is the baseline plus
# USD 200.00. The order names the allocation as the one stop, so the three
# project design limits that would end the run first move with it in one
# change set: the away submission count, the accepted-question target and the
# construction review checkpoint, which becomes equal to the ceiling (the
# captain's order is the review). The evaluation phase keeps its own ceiling.
# Halt at exhaustion, no reset, no replay.
CHAPTER3_EXPANSION_ALLOCATION_USD = Decimal("200.00")
CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD = (
    CHAPTER3_CONSTRUCTION_SPEND_BEFORE_USD + CHAPTER3_EXPANSION_ALLOCATION_USD
)
CHAPTER3_EXPANSION_MAXIMUM_SUBMISSIONS = 20000
CHAPTER3_EXPANSION_ACCEPTED_TARGET = 2000
CHAPTER3_EXPANSION_CHANGE = {
    "away_session_total_ceiling_usd": {
        "from": str(CHAPTER3_CUMULATIVE_CEILING_USD),
        "to": str(CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD),
    },
    "away_maximum_generation_submissions": {
        "from": 5000,
        "to": CHAPTER3_EXPANSION_MAXIMUM_SUBMISSIONS,
    },
    "accepted_question_target": {
        "from": 500,
        "to": CHAPTER3_EXPANSION_ACCEPTED_TARGET,
    },
    "construction_review_checkpoint_usd": {
        "from": "250.00",
        "to": str(CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD),
    },
}
# Chapter 3 paper concurrency (captain order 2026-09-16 21:20 UTC): the
# producer runs several papers at once, so the request rate the run can reach
# is no longer one call at a time. The two request-rate limits move together
# and nothing else moves: the money ceilings, the per-request cap, the paper
# cost cap and every project design count stay exactly as the expansion left
# them. The pair is registered, so only these two exact values are admitted.
CHAPTER3_CONCURRENCY_REQUESTS = 8
CHAPTER3_CONCURRENCY_REQUESTS_PER_MINUTE = 40
CHAPTER3_CONCURRENCY_CHANGE = {
    "maximum_concurrent_generation_requests": {
        "from": 2,
        "to": CHAPTER3_CONCURRENCY_REQUESTS,
    },
    "maximum_generation_requests_per_minute": {
        "from": 10,
        "to": CHAPTER3_CONCURRENCY_REQUESTS_PER_MINUTE,
    },
}
# Chapter 3 parallel bookkeeping (captain order 2026-09-17 03:35 UTC): sixteen
# papers in flight. The ledger no longer pays for the whole history on every
# call, so the request rate the run can reach moves with it. The two
# request-rate limits move together and nothing else moves: the money
# ceilings, the per-request cap, the paper cost cap and every project design
# count stay exactly as the expansion left them.
CHAPTER3_PARALLEL_REQUESTS = 16
CHAPTER3_PARALLEL_REQUESTS_PER_MINUTE = 100
CHAPTER3_PARALLEL_CHANGE = {
    "maximum_concurrent_generation_requests": {
        "from": CHAPTER3_CONCURRENCY_REQUESTS,
        "to": CHAPTER3_PARALLEL_REQUESTS,
    },
    "maximum_generation_requests_per_minute": {
        "from": CHAPTER3_CONCURRENCY_REQUESTS_PER_MINUTE,
        "to": CHAPTER3_PARALLEL_REQUESTS_PER_MINUTE,
    },
}
# Chapter 3 fifty in flight (captain order 2026-09-17 06:25 UTC). The warm
# ledger proof no longer walks the whole history, so the serialised
# bookkeeping of one paid call fell from about 2.4 s to under 0.2 s and the
# request rate the run can reach moves with it. The two request-rate limits
# move together and nothing else moves: the money ceilings, the per-request
# cap, the paper cost cap and every project design count stay exactly as the
# expansion left them.
CHAPTER3_SCALE_REQUESTS = 50
CHAPTER3_SCALE_REQUESTS_PER_MINUTE = 300
CHAPTER3_SCALE_CHANGE = {
    "maximum_concurrent_generation_requests": {
        "from": CHAPTER3_PARALLEL_REQUESTS,
        "to": CHAPTER3_SCALE_REQUESTS,
    },
    "maximum_generation_requests_per_minute": {
        "from": CHAPTER3_PARALLEL_REQUESTS_PER_MINUTE,
        "to": CHAPTER3_SCALE_REQUESTS_PER_MINUTE,
    },
}
# The registered request-rate pairs, in the order they were authorized.
ALLOWED_REQUEST_RATES = (
    (2, 10),
    (CHAPTER3_CONCURRENCY_REQUESTS, CHAPTER3_CONCURRENCY_REQUESTS_PER_MINUTE),
    (CHAPTER3_PARALLEL_REQUESTS, CHAPTER3_PARALLEL_REQUESTS_PER_MINUTE),
    (CHAPTER3_SCALE_REQUESTS, CHAPTER3_SCALE_REQUESTS_PER_MINUTE),
)
POLICY_TRANSITION_CHANGES = (
    {"live_test_maximum_papers": {"from": 20, "to": 40}},
    {
        "live_test_maximum_papers": {"from": 40, "to": 41},
        "live_test_maximum_generation_submissions": {"from": 100, "to": 101},
    },
    UNBOUNDED_COUNT_CHANGE,
    LIVE_TEST_BUDGET_EXTENSION_CHANGE,
    PRODUCTION_BUDGET_EXTENSION_CHANGE,
    CHAPTER2_BUDGET_EXTENSION_CHANGE,
    CHAPTER3_BUDGET_CHANGE,
    CHAPTER3_EXPANSION_CHANGE,
    CHAPTER3_CONCURRENCY_CHANGE,
    CHAPTER3_PARALLEL_CHANGE,
    CHAPTER3_SCALE_CHANGE,
)
# The policy transitions that move the construction ceiling. Each one binds a
# complete stream-input gate and names its own cumulative ceiling as the tranche.
CEILING_CHANGES = (
    PRODUCTION_BUDGET_EXTENSION_CHANGE,
    CHAPTER2_BUDGET_EXTENSION_CHANGE,
    CHAPTER3_BUDGET_CHANGE,
    CHAPTER3_EXPANSION_CHANGE,
)
CEILING_EXTENSION_CHANGE: dict[str, Any] = {}
AUTHORIZED_CAP_REASON = "the paid request exceeds the authorized live-test cap"
PER_REQUEST_CAP_REASON = "the paid request exceeds USD 0.25"
# One paper family reached ``maximum_paper_cost_usd``. The refusal describes the
# family, not the moment and not the run: the cap holds, nothing is charged past
# it, and every other family keeps its own budget. The producer records the
# family and moves to the next paper instead of ending the run.
PAPER_COST_CAP_REASON = "the paid request exceeds the paper cost limit"
# Two scheduling refusals describe the moment, not the request: another request
# of the same phase holds a slot or the window. ``execute`` waits a bounded time
# for room before it records one. A request that one of them stopped resumes
# under the next reviewed transition, like a request the live-test cap stopped
# (the chapter 3 run of 2026-09-16 10:00 UTC lost a request this way when two
# evaluation requests held the shared slots).
CONCURRENCY_LIMIT_REASON = "the paid-call concurrency limit is complete"
MINUTE_LIMIT_REASON = "the paid-call minute limit is complete"
TRANSIENT_RESERVATION_REASONS = (CONCURRENCY_LIMIT_REASON, MINUTE_LIMIT_REASON)
RESUMABLE_NOT_SUBMITTED_REASONS = (
    AUTHORIZED_CAP_REASON,
    *TRANSIENT_RESERVATION_REASONS,
)
TRANSIENT_RESERVATION_RETRY_SECONDS = 90.0
TRANSIENT_RESERVATION_RETRY_INTERVAL_SECONDS = 3.0
# An ordinary request waits this long for the exclusive operation lock that a
# reviewed operation holds. A reviewed authorization, a batch activation or a
# reconciliation takes the lock for a moment; a request that meets one is not
# about that operation and has nothing to report, so it waits instead of ending
# the run. The chapter 3 producer exited on that immediate refusal at 18:26 UTC
# on 2026-09-16 while a release of another task held the lock.
OPERATION_LOCK_WAIT_SECONDS = 120.0
OPERATION_LOCK_WAIT_INTERVAL_SECONDS = 1.0
# A concurrent request holds the operation lock only through its admission, so
# the next request is usually waiting for a lock that frees within
# milliseconds. A one-second poll would serialise the admissions at one a
# second whatever the policy allows, so the concurrent path polls finely. The
# bound and the refusal are unchanged.
OPERATION_LOCK_CONCURRENT_WAIT_INTERVAL_SECONDS = 0.01
OPERATION_LOCK_BUSY_REASON = "another paid broker operation is active"
# Under paper concurrency the exclusive operation lock is a queue, not a race.
# Four paper threads meet it on every ledger mutation, so a thread that finds it
# held is behind a peer that will free it, not in front of a fault. It therefore
# waits for as long as the queue needs. The ceiling below is a sanity bound in
# minutes: a lock still held after it means a process died with the lock or a
# reviewed operation is stuck, and even then the request is retried under the
# same request key before the wait gives up. 19 papers were faulted with
# ``BrokerOperationBusyError`` in the first 40 minutes of the concurrent chapter
# 3 run on 2026-09-16 because the bound was 120 seconds and the exclusive
# section held the free token count and the pacing sleep.
OPERATION_LOCK_QUEUE_CEILING_SECONDS = 600.0
OPERATION_LOCK_QUEUE_ROUNDS = 3
# The wait is long, so it says so. One line every 30 seconds names the section
# and the seconds waited, and every section reports its wait and its hold when
# it ends, which is how the lock is measured without a profiler.
OPERATION_LOCK_HEARTBEAT_SECONDS = 30.0
# A section whose wait and hold are both below this is ordinary and stays out of
# the log; the run makes thousands of them.
OPERATION_LOCK_LOG_THRESHOLD_SECONDS = 1.0
# The immutable-event proof of one ledger row is kept until the row moves, and
# a full pass over every row runs again at least this often. The proof covers
# receipts that were written immutable and never change, so replaying it on
# every one of the seven ledger reads a paid call makes was pure cost: 2.4
# seconds a read at 5,393 rows and 21,508 receipt files, which is what
# serialised the four paper threads of the concurrent chapter 3 run.
IMMUTABLE_EVENT_REVALIDATION_SECONDS = 300.0
# A request that reached the end of its life: nothing is owed but custody of
# its immutable final receipt.
TERMINAL_REQUEST_STATES = frozenset({"completed", "ambiguous_charge"})
# A concurrent broker re-lists the receipts directory at most this often. Under
# paper concurrency the directory moves on every paid call of every worker, so
# the directory fingerprint alone made a 32,251-entry ``scandir`` part of
# nearly every ledger read. A sequential broker keeps the exact fingerprint and
# passes 0.0 here.
RECEIPT_LISTING_REFRESH_SECONDS = 10.0
# The name of an immutable paid-call receipt: the request key, an optional
# resume or count-retry qualifier, and an optional stage.
PAID_CALL_RECEIPT_NAME = re.compile(
    r"([a-f0-9]{64})"
    r"(?:\.resume-[a-f0-9]{64}|\.count-retry-[1-9][0-9]*)?"
    r"(?:\.(?:submitted|received))?\.json"
)
ALLOWED_LIVE_TEST_LIMITS = {(20, 100), (40, 100), (41, 101), (None, None)}
STREAM_INPUT_BINDING_VERSION = "stream-input-binding-v1"
TRANSITION_GATE_SUCCESSOR_FIELDS = {
    "execution_gate_sha256",
    "integrated_code_commit",
    "review_record",
    "review_record_sha256",
}
PRETRANSPORT_SETTLEMENT_SCHEMA = "shared-paid-call-pretransport-settlement-v1"
PHASELESS_REFUSAL_SETTLEMENT_SCHEMA = "shared-paid-call-phaseless-refusal-settlement-v1"
PHASELESS_REFUSAL_INTEGRITY_HALT_REASON = (
    "ValueError: the configuration transition ledger hash changed"
)
# Orphan recovery read the ledger once and then settled each request it found.
# Another worker of the same ledger can settle one of those requests inside
# that window, so the row is no longer ``submitted`` when the settlement runs.
# The recovery records the skip beside the receipts and continues; the other
# worker's settlement is the accounting record (chapter 3 producer exit of
# 2026-09-16 13:53 UTC).
SETTLE_SKIPPED_SCHEMA = "shared-paid-call-settle-skipped-v1"
COUNT_ERROR_CONTINUATION_SCHEMA = "shared-paid-call-count-error-continuation-v1"
COUNT_ERROR_CONTINUATION_EVIDENCE_SCHEMA = (
    "shared-paid-call-count-error-continuation-evidence-v1"
)
# ``countTokens`` is free: it reserves nothing, submits nothing and charges
# nothing, so a failure of it can never make the money uncertain. A provider
# fault there is therefore retried in place before it becomes a count error.
# The retry is bounded: five attempts with a jittered exponential backoff of
# about two minutes in total, which is the same order as the wait an ordinary
# request already gives the exclusive operation lock.
TRANSIENT_COUNT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
PERMANENT_COUNT_HTTP_STATUSES = frozenset({400, 401, 403, 404})
COUNT_RETRY_ATTEMPTS = 5
COUNT_RETRY_BASE_SECONDS = 8.0
COUNT_RETRY_MAXIMUM_SECONDS = 64.0
COUNT_RETRY_JITTER = 0.2
# A count failure that is transient describes the provider at that moment, not
# the request and not the run, so it halts nothing. The ledger row keeps the
# ``count_error`` state and records this class beside it; the producer skips the
# family and a later visit counts again under a new retry round. A count failure
# that is permanent means the request or the credential is wrong, so it keeps
# the halt it has always had.
TRANSIENT_COUNT_FAILURE = "transient"
PERMANENT_COUNT_FAILURE = "permanent"
COUNT_UNAVAILABLE_REASON = "the free countTokens preflight stayed unavailable"
AMBIGUOUS_CONTINUATION_SCHEMA = "shared-paid-call-ambiguous-continuation-v1"
AMBIGUOUS_CONTINUATION_EVIDENCE_SCHEMA = (
    "shared-paid-call-ambiguous-continuation-evidence-v1"
)
RECEIVED_MAX_TOKENS_CONTINUATION_SCHEMA = (
    "shared-paid-call-received-max-tokens-continuation-v1"
)
RECEIVED_MAX_TOKENS_CONTINUATION_EVIDENCE_SCHEMA = (
    "shared-paid-call-received-max-tokens-continuation-evidence-v1"
)
PROVIDER_TIMEOUT_CONTINUATION_SCHEMA = (
    "shared-paid-call-provider-timeout-continuation-v1"
)
PROVIDER_TIMEOUT_CONTINUATION_EVIDENCE_SCHEMA = (
    "shared-paid-call-provider-timeout-continuation-evidence-v1"
)
# The exact string the transport failure path writes for a timeout. The client
# stopped waiting; the provider may have finished and billed the call.
PROVIDER_TIMEOUT_ERROR = "TimeoutError: provider outcome unknown"
PROVIDER_TIMEOUT_ERROR_CLASS = "provider_timeout_unknown_charge"
PROVIDER_TIMEOUT_SKIP_REASON = "operational_ambiguous_charge_provider_timeout"
AMBIGUOUS_CONTINUATION_RESERVATION_POLICY = (
    "retain_full_reservation_in_ambiguous_reserved_and_count_against_all_caps"
)
# A provider rejection before generation: the request never reached the model,
# so the provider bills nothing. The recorded error body, or a reproduction of
# the same request with the same rejection, is the evidence that settles it.
HTTP_REJECTION_SETTLEMENT_SCHEMA = "shared-paid-call-http-rejection-settlement-v1"
HTTP_REJECTION_EVIDENCE_SCHEMA = "shared-paid-call-http-rejection-evidence-v1"
HTTP_REJECTION_STATUSES = frozenset({400})
HTTP_REJECTION_PROVIDER_STATUSES = frozenset({"INVALID_ARGUMENT"})
HTTP_ERROR_BODY_LIMIT = 4000
ORPHANED_CONTINUATION_SCHEMA = "shared-paid-call-orphaned-continuation-v1"
ORPHANED_CONTINUATION_EVIDENCE_SCHEMA = (
    "shared-paid-call-orphaned-continuation-evidence-v1"
)
ORPHANED_CONTINUATION_RESERVATION_POLICY = (
    "retain_full_reservation_in_reserved_and_count_against_all_caps"
)
AMBIGUOUS_CONTINUATION_FIELDS = {
    "schema",
    "request_key",
    "ambiguous_receipt_sha256",
    "request_identity",
    "error_class",
    "http_status",
    "live_call_made",
    "received_receipt_absent",
    "reserved_usd",
    "reservation_policy",
    "scope",
    "affected_family_id",
    "skip_reason_code",
    "authorized_run_id",
    "evidence_file",
    "evidence_file_sha256",
    "review_file",
    "review_file_sha256",
    "ledger_sha256_before",
    "gate_sha256",
    "integrated_code_commit",
    "authorized_at_utc",
    "operator_id",
}
RECEIVED_MAX_TOKENS_CONTINUATION_FIELDS = AMBIGUOUS_CONTINUATION_FIELDS - {
    "http_status",
    "received_receipt_absent",
} | {
    "received_receipt_sha256",
    "request_trace_sha256",
    "finish_reason",
    "received_receipt_present",
}
PROVIDER_TIMEOUT_CONTINUATION_FIELDS = AMBIGUOUS_CONTINUATION_FIELDS - {
    "http_status"
} | {"error", "timeout_seconds"}
HTTP_REJECTION_SETTLEMENT_FIELDS = {
    "schema",
    "request_key",
    "ambiguous_receipt_sha256",
    "request_identity",
    "http_status",
    "provider_error_status",
    "error_body_source",
    "reproduction_record",
    "reproduction_record_sha256",
    "reserved_usd",
    "actual_cost_usd",
    "live_call_made",
    "generation_started",
    "replay_prohibited",
    "evidence_file",
    "evidence_file_sha256",
    "review_file",
    "review_file_sha256",
    "ledger_sha256_before",
    "gate_sha256",
    "integrated_code_commit",
    "authorized_run_id",
    "operator_id",
    "settled_at_utc",
}
_ZERO_USAGE = {
    "promptTokenCount": 0,
    "candidatesTokenCount": 0,
    "thoughtsTokenCount": 0,
}
ORPHANED_CONTINUATION_FIELDS = {
    "schema",
    "request_key",
    "submitted_receipt_sha256",
    "request_trace_sha256",
    "request_identity",
    "reserved_usd",
    "reservation_policy",
    "scope",
    "affected_family_id",
    "skip_reason_code",
    "authorized_run_id",
    "evidence_file",
    "evidence_file_sha256",
    "review_file",
    "review_file_sha256",
    "ledger_sha256_before",
    "gate_sha256",
    "integrated_code_commit",
    "authorized_at_utc",
    "operator_id",
}
PRETRANSPORT_SETTLEMENT_REQUEST = {
    "request_key": "445c8935c5dc9d1c5d03fe7d4d15308fd57d0e68f8d2d21bf310875b85b5512b",
    "run_id": "first-production-6fbdf41-r1",
    "stage": "finding_answer_extraction",
    "paper_id": "10.1007/s44295-026-00097-4",
    "family_id": "family-c44489994cd247de1375",
    "state": "submitted",
    "reserved_usd": "0.036094",
}
# The one refusal row that the reviewed phase settlement of 2026-09-16 may
# repair. The streaming evaluator ran from snapshot `a0b9a82`, which predates
# the phase-scoped ledger form of `183779b`, so its `not_submitted` refusals
# carry no `phase`. `_only_evaluation_activity_since` reads a row without a
# phase as construction, so this one row made every later broker start refuse
# the applied configuration transition and write an integrity halt. The row
# made no provider call and holds no money, and its own fields prove the
# phase: the stage prefix, the evaluation trial and the evaluation gate.
PHASELESS_REFUSAL_SETTLEMENT_REQUEST = {
    "request_key": "52c5da7533e8d24f36e24e73d718cfaa06d928d64fedf6f5cfeee99a9f745ca9",
    "run_id": "abstention-stream-r10-aqa-7f09e4bdf6bac5c50d4c",
    "stage": "evaluation_answer:gemini-3.8-flash",
    "paper_id": "aqa-7f09e4bdf6bac5c50d4c",
    "family_id": "evaluation-item:aqa-7f09e4bdf6bac5c50d4c",
    "state": "not_submitted",
    "reason": "the evaluation repeat limit for this item is complete",
    "completed_at_utc": "2026-09-16T23:03:53Z",
}
STREAM_INPUT_GATE_FIELDS = {
    "continuation_artifact",
    "continuation_access_run_id",
    "continuation_run_manifest_sha256",
    "continuation_run_receipt_sha256",
    "continuation_frozen_manifest_sha256",
    "continuation_order_sha256",
    "continuation_family_count",
    "authorized_new_run_id",
    "authorized_campaign_id",
    "eligibility_prompt_sha256",
    "eligibility_schema_sha256",
    "eligibility_policy_sha256",
}
USAGE_RECONCILIATION_FIELDS = {
    "schema",
    "request_key",
    "received_receipt_sha256",
    "ambiguous_receipt_sha256",
    "config_transition_sha256",
    "price_config_sha256",
    "normalized_usage",
    "actual_cost_usd",
    "ledger_sha256_before",
    "gate_sha256",
    "integrated_code_commit",
    "review_record",
    "review_record_sha256",
    "reconciled_at_utc",
}
# Two fields that an event written before 2026-09-16 does not carry: which
# token count the provider omitted, and the phase of the request. An older
# event stays valid without them.
USAGE_RECONCILIATION_OPTIONAL_FIELDS = {"omitted_zero_usage_field", "phase"}
EXCLUSIVE_BATCH_SCHEMA = "shared-gemini-exclusive-batch-v1"
ACCEPTED_ITEM_V1_SCHEMA = "shared-paid-call-accepted-item-v1"
ACCEPTED_ITEM_V2_SCHEMA = "shared-paid-call-accepted-item-v2"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _is_received_max_tokens_ambiguous_case(
    final: dict[str, Any],
    received: dict[str, Any],
    trace: dict[str, Any],
    request: dict[str, Any],
) -> bool:
    if not all(isinstance(value, dict) for value in (final, received, trace, request)):
        return False
    response = received.get("response")
    if not isinstance(response, dict):
        return False
    candidates = response.get("candidates")
    usage = response.get("usageMetadata")
    if (
        not isinstance(candidates, list)
        or candidates != [{"content": {}, "finishReason": "MAX_TOKENS", "index": 0}]
        or not isinstance(usage, dict)
        or "candidatesTokenCount" in usage
        or "thoughtsTokenCount" in usage
        or isinstance(usage.get("promptTokenCount"), bool)
        or not isinstance(usage.get("promptTokenCount"), int)
        or usage.get("totalTokenCount") != usage.get("promptTokenCount")
    ):
        return False
    payload = trace.get("payload")
    generation = payload.get("generationConfig") if isinstance(payload, dict) else None
    return (
        final.get("state") == "ambiguous_charge"
        # The chapter 2 receipt recorded the message of the one-field rule; a
        # response with both counts absent is inconsistent under the two-field
        # rule of 2026-09-16. The shape test above is the same for both.
        and final.get("error")
        in (
            "ValueError: provider usage cannot prove zero thinking tokens",
            "ValueError: provider usage is inconsistent",
        )
        and final.get("live_call_made") is True
        and final.get("response") == response
        and final.get("reserved_usd") == request.get("reserved_usd")
        and final.get("actual_cost_usd") is None
        and received.get("state") == "response_received"
        and received.get("live_call_made") is True
        and received.get("response") == response
        and trace.get("request_key") == request.get("request_key")
        and trace.get("request_sha256") == request.get("request_sha256")
        and trace.get("stage") == "answer_agreement"
        and trace.get("model") == "gemini-3.1-flash-lite"
        and generation
        == {
            "candidateCount": 1,
            "maxOutputTokens": 4,
            "responseJsonSchema": {"enum": ["yes", "no"], "type": "string"},
            "responseMimeType": "text/x.enum",
            "temperature": 0,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        }
        and request.get("stage") == "answer_agreement"
        and request.get("model") == "gemini-3.1-flash-lite"
    )


def _is_provider_timeout_ambiguous_case(
    final: dict[str, Any],
    request: dict[str, Any],
    *,
    received_receipt_present: bool,
) -> bool:
    """Say whether one receipt is the bounded provider-timeout ambiguous case.

    The client stopped waiting before the provider answered. Nothing came back,
    so the charge is unknown in exactly the way an HTTP 5xx answer is unknown:
    the call went out live and no usage was ever received. The receipt must
    carry no response, no HTTP outcome and no settled cost, which keeps this
    case disjoint from the 5xx case and from the received-max-tokens case.
    """
    if not isinstance(final, dict) or not isinstance(request, dict):
        return False
    return (
        final.get("state") == "ambiguous_charge"
        and final.get("error") == PROVIDER_TIMEOUT_ERROR
        and final.get("live_call_made") is True
        and "response" not in final
        and "error_class" not in final
        and "http_status" not in final
        and not received_receipt_present
        and final.get("reserved_usd") == request.get("reserved_usd")
        and final.get("actual_cost_usd") is None
    )


def _receipt_timeout_seconds(final: dict[str, Any]) -> int:
    """Return the timeout the timed-out call actually ran under.

    A receipt written before the timeout became a stage model fact names none.
    Such a call ran under the one fixed transport timeout, so that value is
    reported. The current configuration is never substituted: the stage may
    carry a longer timeout today than the call that timed out was given.
    """
    value = final.get("timeout_seconds", DEFAULT_CALL_TIMEOUT_SECONDS)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAXIMUM_CALL_TIMEOUT_SECONDS
    ):
        raise ValueError("the ambiguous receipt call timeout is invalid")
    return value


def exclusive_batch_marker_path(ledger_file: Path) -> Path:
    return ledger_file.resolve().with_name(f".{ledger_file.name}.exclusive-batch.json")


def hold_operation_lock(
    path: Path,
    *,
    wait_seconds: float = 0.0,
    busy_error: type[ValueError] = ValueError,
    poll_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
    heartbeat: Callable[[float], None] | None = None,
) -> Any:
    """Open the exclusive operation lock file and hold it, or refuse.

    ``wait_seconds`` is the bound of the wait for a lock another operation
    holds, and ``busy_error`` is what the bound raises. The two travel
    together, so the meaning of a refusal never depends on the value of a
    tunable constant.

    A reviewed operation takes the default of both: no wait, and a plain
    ``ValueError`` that the broker seam marks a whole-run stop. Two reviewed
    operations of one ledger must never overlap, and the second one has an
    operator to tell. The one ordinary request path passes a wait and
    :class:`BrokerOperationBusyError` instead, because a reviewed operation is
    short and the request describes no fault of its own.

    ``heartbeat`` is called with the seconds waited so far, every
    ``heartbeat_seconds`` of waiting. A long wait is normal under paper
    concurrency, so it is reported while it happens rather than only when it
    ends.
    """
    handle = path.open("a+")
    started = time.monotonic()
    deadline = started + wait_seconds
    next_heartbeat = (
        started + heartbeat_seconds
        if heartbeat is not None and heartbeat_seconds
        else None
    )
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError as error:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                handle.close()
                raise busy_error(OPERATION_LOCK_BUSY_REASON) from error
            if next_heartbeat is not None and now >= next_heartbeat:
                heartbeat(now - started)  # type: ignore[misc]
                next_heartbeat = now + float(heartbeat_seconds)  # type: ignore[arg-type]
            interval = (
                OPERATION_LOCK_WAIT_INTERVAL_SECONDS
                if poll_seconds is None
                else poll_seconds
            )
            time.sleep(min(interval, remaining))


def activate_exclusive_batch_mode(
    ledger_file: Path,
    *,
    batch_identity: str,
    batch_state_file: Path,
    expected_ledger_sha256: str,
) -> dict[str, Any]:
    ledger_file = ledger_file.resolve()
    batch_state_file = batch_state_file.resolve()
    if not re.fullmatch(r"[a-f0-9]{64}", batch_identity):
        raise ValueError("the exclusive batch identity is invalid")
    if not batch_state_file.is_file():
        raise ValueError("the exclusive batch state file is absent")
    operation_path = ledger_file.with_name(f".{ledger_file.name}.operation.lock")
    ledger_lock_path = ledger_file.with_name(f".{ledger_file.name}.lock")
    marker_path = exclusive_batch_marker_path(ledger_file)
    with hold_operation_lock(operation_path):
        with ledger_lock_path.open("a+") as ledger_lock:
            fcntl.flock(ledger_lock, fcntl.LOCK_EX)
            if sha256_file(ledger_file) != expected_ledger_sha256:
                raise ValueError(
                    "the shared paid-call ledger changed before batch activation"
                )
            # The file is the compacted snapshot; the state is the store.
            ledger = ledger_store.read_ledger(ledger_file)
            if (
                ledger.get("schema") != "shared-paid-call-ledger-v1"
                or ledger.get("halted") is not False
                or ledger.get("inflight") != 0
                or _money(ledger.get("reserved_usd"), "reserved") != 0
                or _money(ledger.get("ambiguous_reserved_usd"), "ambiguous") != 0
            ):
                raise ValueError("the shared paid-call ledger is not settled")
            marker = {
                "schema": EXCLUSIVE_BATCH_SCHEMA,
                "batch_identity": batch_identity,
                "batch_state_file": str(batch_state_file),
                "batch_state_sha256_at_activation": sha256_file(batch_state_file),
                "shared_ledger_file": str(ledger_file),
                "shared_ledger_sha256_at_activation": expected_ledger_sha256,
                "activated_at_utc": _now(),
            }
            if marker_path.is_file():
                existing = _read(marker_path)
                comparable = dict(marker)
                comparable["batch_state_sha256_at_activation"] = existing.get(
                    "batch_state_sha256_at_activation"
                )
                comparable["activated_at_utc"] = existing.get("activated_at_utc")
                if existing != comparable:
                    raise ValueError(
                        "another exclusive batch mode owns the shared ledger"
                    )
                return existing
            atomic_json(marker_path, marker, immutable=True)
            return marker


def _omitted_zero_usage_field(usage: dict[str, Any]) -> str | None:
    """Return the one token field the provider omitted as zero, or None.

    The provider can omit a token count when its value is zero. The broker
    accepts that only when the other three counts are nonnegative integers and
    the recorded total proves the omitted value is zero. Two shapes occur:

    - ``thoughtsTokenCount`` absent, with ``total == prompt + candidates``.
    - ``candidatesTokenCount`` absent, with ``total == prompt + thoughts``.
      gemini-3.7-flash returned that shape on 2026-09-16 for a one-letter
      answer (research/arctic-abstention-streaming-eval-r1/report.md, section 7).

    Anything else is an inconsistent usage record and stays an ambiguous
    charge.
    """
    for omitted, others in (
        ("thoughtsTokenCount", ("promptTokenCount", "candidatesTokenCount")),
        ("candidatesTokenCount", ("promptTokenCount", "thoughtsTokenCount")),
    ):
        if omitted in usage:
            continue
        values = [usage.get(name) for name in (*others, "totalTokenCount")]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            return None
        if values[2] == values[0] + values[1]:
            return omitted
        return None
    return None


OMITTED_ZERO_REASON = {
    "thoughtsTokenCount": "provider usage cannot prove zero thinking tokens",
    "candidatesTokenCount": "provider usage cannot prove zero answer tokens",
}


def _normalized_usage(response: Any) -> dict[str, Any]:
    usage = response.get("usageMetadata") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("provider usage is absent")
    normalized = dict(usage)
    absent = [
        name
        for name in ("thoughtsTokenCount", "candidatesTokenCount")
        if name not in normalized
    ]
    if absent:
        # The broker fills one omitted count that the recorded total proves is
        # zero. Two omitted counts prove nothing, so the record stays
        # unusable. The message is keyed on the first absent name, which puts
        # the thinking count first: the reviewed chapter 2 continuation of the
        # `answer_agreement` MAX_TOKENS incident binds that exact string, and
        # that receipt carries neither count.
        omitted = _omitted_zero_usage_field(normalized) if len(absent) == 1 else None
        if omitted is None:
            raise ValueError(OMITTED_ZERO_REASON[absent[0]])
        normalized[omitted] = 0
    names = (
        "promptTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
    )
    values = [normalized.get(name) for name in names]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ) or values[3] != sum(values[:3]):
        raise ValueError("provider usage is inconsistent")
    return normalized


def _money(value: Any, name: str, *, positive: bool = False) -> Decimal:
    # The import is resolved once, not on every call. The ledger comparison
    # calls this about 6,200 times per proof of one moved row, and the
    # function-local import was 17 percent of that comparison on 2026-09-17.
    return _decimal(value, name, positive=positive)


def is_evaluation_stage(stage: Any) -> bool:
    """Return whether a stage belongs to the evaluation stage family."""
    return (
        isinstance(stage, str) and EVALUATION_STAGE_PATTERN.fullmatch(stage) is not None
    )


def stage_supported(stage: Any) -> bool:
    return stage in STAGES or is_evaluation_stage(stage)


def evaluation_stage(model: str) -> str:
    """Return the ledger stage of one evaluated model."""
    stage = f"{EVALUATION_STAGE_PREFIX}{model}"
    if not is_evaluation_stage(stage):
        raise ValueError(f"the evaluated model name cannot form a stage: {model}")
    return stage


def _validate_evaluation_policy(
    path: Path, construction_policy: dict[str, Any]
) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != EVALUATION_POLICY_SCHEMA:
        raise ValueError("unsupported benchmark evaluation policy schema")
    if not str(value.get("policy_id") or "").strip():
        raise ValueError("the benchmark evaluation policy lacks a policy id")
    ceiling = _money(
        value.get("evaluation_ceiling_usd"), "evaluation_ceiling_usd", positive=True
    )
    reserve = _money(
        construction_policy["reserved_for_benchmark_evaluation_usd"],
        "evaluation reserve",
    )
    if ceiling > reserve:
        raise ValueError("the evaluation ceiling exceeds the evaluation reserve")
    request_cap = _money(
        value.get("maximum_request_reserved_cost_usd"),
        "maximum_request_reserved_cost_usd",
        positive=True,
    )
    if request_cap > _money(
        construction_policy["maximum_request_reserved_cost_usd"], "request cap"
    ):
        raise ValueError("the evaluation request cap exceeds the construction cap")
    for field in (
        "maximum_calls_per_item_condition_model_arm",
        "maximum_concurrent_requests",
        "maximum_requests_per_minute",
        "maximum_output_tokens_including_thinking",
    ):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"the benchmark evaluation policy {field} is invalid")
    if value["maximum_output_tokens_including_thinking"] > int(
        construction_policy["maximum_output_tokens_including_thinking"]
    ):
        raise ValueError("the evaluation output limit exceeds the construction limit")
    if value.get("automatic_transport_retries") != 0:
        raise ValueError("the benchmark evaluation policy permits retries")
    for field, expected in {
        "automatic_model_fallback": False,
        "re_ask_on_invalid_response": False,
        "automatic_budget_rearm": False,
        "stop_on_first_infrastructure_error_or_ambiguous_charge": True,
    }.items():
        if value.get(field) is not expected:
            raise ValueError(f"benchmark evaluation control changed: {field}")
    return value


def _validate_evaluation_price_config(
    path: Path, construction_config: dict[str, Any]
) -> dict[str, Any]:
    from datetime import date

    value = _read(path)
    if value.get("schema") != EVALUATION_PRICE_CONFIG_SCHEMA:
        raise ValueError("unsupported benchmark evaluation price config schema")
    if not str(value.get("config_id") or "").strip():
        raise ValueError("the benchmark evaluation price config lacks a config id")
    if value.get("api_base") != construction_config["api_base"]:
        raise ValueError("the benchmark evaluation API base differs from the broker")
    if value.get("provider") != "google_gemini":
        raise ValueError(
            "the benchmark evaluation price config provider is unsupported"
        )
    models = value.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("the benchmark evaluation price config lists no model")
    for model, entry in models.items():
        if not is_evaluation_stage(f"{EVALUATION_STAGE_PREFIX}{model}"):
            raise ValueError(f"the evaluated model name is invalid: {model}")
        if not isinstance(entry, dict):
            raise ValueError(f"the price entry of {model} is not an object")
        for field in ("maximum_input_tokens", "maximum_output_tokens"):
            if (
                isinstance(entry.get(field), bool)
                or not isinstance(entry.get(field), int)
                or entry[field] < 1
            ):
                raise ValueError(f"the price entry of {model} has an invalid {field}")
        if entry["maximum_output_tokens"] > int(
            construction_config["maximum_output_tokens"]
        ):
            raise ValueError(f"the output limit of {model} exceeds the broker limit")
        for field in (
            "input_usd_per_million_tokens",
            "output_usd_per_million_tokens_including_thinking",
        ):
            _money(entry.get(field), field, positive=True)
        _money(entry.get("temperature"), "temperature")
        levels = entry.get("thinking_levels")
        if (
            not isinstance(levels, list)
            or not levels
            or any(not isinstance(level, str) or not level for level in levels)
        ):
            raise ValueError(f"the price entry of {model} lists no thinking level")
        if not isinstance(entry.get("is_pro"), bool):
            raise ValueError(f"the price entry of {model} lacks the Pro flag")
        if "call_timeout_seconds" in entry:
            from .gemini_eligibility import _validate_call_timeout

            _validate_call_timeout(entry["call_timeout_seconds"])
        start = date.fromisoformat(str(entry.get("price_valid_from")))
        end = date.fromisoformat(str(entry.get("price_valid_through")))
        if not start <= date.today() <= end:
            raise ValueError(f"the price of {model} is not active; update the record")
        if not str(entry.get("price_source", "")).startswith("https://ai.google.dev/"):
            raise ValueError(f"the price source of {model} is not an official URL")
    return value


def evaluation_model_config(config: dict[str, Any], stage: str) -> dict[str, Any]:
    """Return the price and decoding record of one evaluation stage."""
    if not is_evaluation_stage(stage):
        raise ValueError("the stage is not an evaluation stage")
    model = stage[len(EVALUATION_STAGE_PREFIX) :]
    entry = (config.get("models") or {}).get(model)
    if not isinstance(entry, dict):
        raise ValueError(f"the evaluated model has no price entry: {model}")
    return {"api_base": config["api_base"], **entry, "model": model}


def _validate_evaluation_gate(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != EVALUATION_GATE_SCHEMA:
        raise ValueError("unsupported benchmark evaluation gate schema")
    if value.get("evaluation_enabled") is not True:
        raise ValueError("benchmark evaluation is disabled")
    if value.get("allowed_phase") != EVALUATION_PHASE:
        raise ValueError("the benchmark evaluation gate does not allow this phase")
    if value.get("independent_review_verdict") != "pass":
        raise ValueError("the benchmark evaluation review did not pass")
    for field in ("integrated_code_commit", "review_record"):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"the benchmark evaluation gate lacks {field}")
    missing = sorted(EVALUATION_GATE_BINDING_FIELDS - set(value))
    if missing:
        raise ValueError(f"the benchmark evaluation gate lacks {missing[0]}")
    if not isinstance(value["models"], list) or not value["models"]:
        raise ValueError("the benchmark evaluation gate lists no model")
    if not isinstance(value["arms"], list) or not value["arms"]:
        raise ValueError("the benchmark evaluation gate lists no thinking arm")
    repeats = value["repeats_maximum"]
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("the benchmark evaluation gate repeat limit is invalid")
    if not isinstance(value["decoding"], dict):
        raise ValueError("the benchmark evaluation gate decoding record is invalid")
    return value


def _validate_policy(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "streaming-dataset-budget-policy-v1":
        raise ValueError("unsupported streaming budget policy schema")
    exact_money = {
        "project_lifetime_ceiling_usd": Decimal("1000"),
        "reserved_for_benchmark_evaluation_usd": Decimal("500"),
        "dataset_construction_allocation_usd": Decimal("500"),
        "maximum_request_reserved_cost_usd": Decimal("0.25"),
        "maximum_paper_cost_usd": Decimal("1"),
    }
    for field, expected in exact_money.items():
        if _money(value.get(field), field, positive=True) != expected:
            raise ValueError(f"streaming budget value changed: {field}")
    checkpoint = _money(
        value.get("construction_review_checkpoint_usd"),
        "construction_review_checkpoint_usd",
        positive=True,
    )
    if checkpoint not in {
        Decimal("250"),
        CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD,
    }:
        raise ValueError(
            "streaming budget value changed: construction_review_checkpoint_usd"
        )
    away_ceiling = _money(
        value.get("away_session_total_ceiling_usd"),
        "away_session_total_ceiling_usd",
        positive=True,
    )
    if away_ceiling not in {
        Decimal("25"),
        Decimal("61.614496"),
        CHAPTER2_CUMULATIVE_CEILING_USD,
        CHAPTER3_CUMULATIVE_CEILING_USD,
        CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD,
    }:
        raise ValueError(
            "streaming budget value changed: away_session_total_ceiling_usd"
        )
    if checkpoint == CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD and away_ceiling != (
        CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD
    ):
        raise ValueError(
            "streaming budget value changed: construction_review_checkpoint_usd"
        )
    live_test_suballocation = _money(
        value.get("live_test_suballocation_usd"),
        "live_test_suballocation_usd",
        positive=True,
    )
    if live_test_suballocation not in {Decimal("10"), Decimal("20")}:
        raise ValueError("streaming budget value changed: live_test_suballocation_usd")
    if live_test_suballocation > _money(away_ceiling, "away_session_total_ceiling_usd"):
        raise ValueError("the live-test budget exceeds the away-session budget")
    exact_int = {
        "maximum_output_tokens_including_thinking": 8192,
        "automatic_transport_generation_retries": 0,
    }
    for field, expected in exact_int.items():
        if value.get(field) != expected:
            raise ValueError(f"streaming budget value changed: {field}")
    # The concurrency slot count and the minute window are one registered
    # pair, so a policy can never raise one of them alone. The refusal names
    # the field that left its registered values, and names the slot count when
    # both are registered values of different pairs.
    request_rate = (
        value.get("maximum_concurrent_generation_requests"),
        value.get("maximum_generation_requests_per_minute"),
    )
    if request_rate not in ALLOWED_REQUEST_RATES:
        registered_slots = {pair[0] for pair in ALLOWED_REQUEST_RATES}
        field = (
            "maximum_generation_requests_per_minute"
            if request_rate[0] in registered_slots
            else "maximum_concurrent_generation_requests"
        )
        raise ValueError(f"streaming budget value changed: {field}")
    # The two project design counts have one registered expansion each, and
    # both move only together with the chapter 3 expansion ceiling.
    expanded = away_ceiling == CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD
    registered_counts = {
        "accepted_question_target": (
            CHAPTER3_EXPANSION_ACCEPTED_TARGET if expanded else 500
        ),
        "away_maximum_generation_submissions": (
            CHAPTER3_EXPANSION_MAXIMUM_SUBMISSIONS if expanded else 5000
        ),
    }
    for field, expected in registered_counts.items():
        if value.get(field) != expected:
            raise ValueError(f"streaming budget value changed: {field}")
    live_test_limits = (
        value.get("live_test_maximum_papers"),
        value.get("live_test_maximum_generation_submissions"),
    )
    if live_test_limits not in ALLOWED_LIVE_TEST_LIMITS:
        raise ValueError("streaming budget live-test limits changed")
    for field, expected in {
        "live_test_included_in_away_ceiling": True,
        "automatic_model_fallback": False,
        "automatic_budget_rearm": False,
        "stop_on_first_infrastructure_error_or_ambiguous_charge": True,
    }.items():
        if value.get(field) is not expected:
            raise ValueError(f"streaming budget control changed: {field}")
    if _money(value["dataset_construction_allocation_usd"], "construction") + _money(
        value["reserved_for_benchmark_evaluation_usd"], "evaluation"
    ) != _money(value["project_lifetime_ceiling_usd"], "lifetime"):
        raise ValueError("construction and evaluation allocations do not balance")
    return value


def _is_server_error_status(value: Any) -> bool:
    """Return whether a status is a provider server error (HTTP 5xx)."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and 500 <= value <= 599
    )


def _count_failure_class(error: BaseException) -> str:
    """Say whether a countTokens failure is the moment or the request.

    ``countTokens`` charges nothing, so this classification is about whether a
    second attempt can succeed, never about money. A server status, a timeout,
    a connection fault and a malformed count answer all describe the provider
    at that moment, so they are transient. A rejection of the request itself -
    a bad argument, a refused or missing credential, an absent model - is
    permanent: it repeats for as long as the request or the credential is
    wrong, and it keeps the halt it has always had.
    """
    if isinstance(error, urllib.error.HTTPError):
        if error.code in TRANSIENT_COUNT_HTTP_STATUSES:
            return TRANSIENT_COUNT_FAILURE
        return PERMANENT_COUNT_FAILURE
    if isinstance(
        error, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)
    ):
        return TRANSIENT_COUNT_FAILURE
    if isinstance(error, (KeyError, TypeError, ValueError)):
        # The provider accepted the request and answered it with something the
        # broker cannot read as a token count. The request was never in doubt.
        return TRANSIENT_COUNT_FAILURE
    return PERMANENT_COUNT_FAILURE


def _recorded_count_failure_class(receipt: dict[str, Any], reason: str) -> str:
    """Return the failure class of a recorded count error, failing closed.

    A receipt written since the bounded count retry records its own class. An
    older receipt records only the error string, so the class is read back from
    the HTTP status inside it. Anything this cannot read is permanent, because
    only a proven transient status is ever counted again without a review of
    the request itself.
    """
    recorded = receipt.get("count_failure_class")
    if recorded in {TRANSIENT_COUNT_FAILURE, PERMANENT_COUNT_FAILURE}:
        return str(recorded)
    match = re.match(r"HTTPError: HTTP Error (\d{3}): ", reason)
    if match and int(match.group(1)) in TRANSIENT_COUNT_HTTP_STATUSES:
        return TRANSIENT_COUNT_FAILURE
    return PERMANENT_COUNT_FAILURE


def count_error_is_transient(receipt: dict[str, Any]) -> bool:
    """Say whether a stored count error may be counted again.

    The count made no call and charged nothing, so a transient one is replayed
    by counting again rather than by returning the old receipt. A receipt
    written before the bounded retry records no class, so the class is read
    back from its error string and fails closed to permanent.
    """
    if (
        receipt.get("state") != "count_error"
        or receipt.get("live_call_made") is not False
    ):
        return False
    reason = str(receipt.get("error") or "")
    return _recorded_count_failure_class(receipt, reason) == TRANSIENT_COUNT_FAILURE


def _count_http_status(error: BaseException) -> int | None:
    """Return the HTTP status of a countTokens failure, when it has one."""
    return error.code if isinstance(error, urllib.error.HTTPError) else None


def _count_retry_delay(attempt: int) -> float:
    """Return the jittered backoff before the next countTokens attempt."""
    delay = min(
        COUNT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), COUNT_RETRY_MAXIMUM_SECONDS
    )
    return delay * random.uniform(1.0 - COUNT_RETRY_JITTER, 1.0 + COUNT_RETRY_JITTER)


def _count_event_stem(request_key: str, count_retry_round: int) -> str:
    """Return the receipt stem of one countTokens round of a request."""
    if count_retry_round:
        return f"{request_key}.count-retry-{count_retry_round}"
    return request_key


def _http_error_body(error: urllib.error.HTTPError) -> str | None:
    """Read a bounded copy of a provider error body; never raise."""
    try:
        raw = error.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    return text[:HTTP_ERROR_BODY_LIMIT]


def _provider_error_status(body: str | None) -> str | None:
    """Return the provider's error status from a JSON error body, if any."""
    if not body:
        return None
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        return None
    error = value.get("error") if isinstance(value, dict) else None
    status = error.get("status") if isinstance(error, dict) else None
    return status if isinstance(status, str) and status else None


def phase_halt_reason(ledger: dict[str, Any], phase: str) -> str | None:
    """Return the halt reason that blocks one phase of a ledger, or None.

    The ledger halt blocks every phase. The evaluation halt blocks the
    evaluation phase only, so an ambiguous evaluation charge never stops the
    construction pipeline. A reader of the ledger, such as the streaming
    evaluator, asks this function whether its own phase can call again.
    """
    if ledger.get("halted"):
        return str(ledger.get("halt_reason") or "halted")
    if phase == EVALUATION_PHASE and ledger.get("evaluation_halted"):
        return str(ledger.get("evaluation_halt_reason") or "halted")
    return None


def _validate_gate(path: Path, phase: str) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "streaming-live-execution-gate-v1":
        raise ValueError("unsupported streaming execution gate schema")
    if value.get("live_generation_enabled") is not True:
        raise ValueError("streaming live generation is disabled")
    if value.get("allowed_phase") != phase:
        raise ValueError("the streaming execution gate does not allow this phase")
    if value.get("independent_review_verdict") != "pass":
        raise ValueError("the integrated offline review did not pass")
    for field in ("integrated_code_commit", "review_record"):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"the streaming execution gate lacks {field}")
    return value


def _gate_succeeds_transition_review(
    gate: dict[str, Any], authorization: dict[str, Any]
) -> bool:
    """Accept a reviewed successor gate without changing an old transition receipt."""
    successor = gate.get("supersedes_config_transition_review")
    if (
        not isinstance(successor, dict)
        or set(successor) != TRANSITION_GATE_SUCCESSOR_FIELDS
    ):
        return False
    if any(successor[field] != authorization[field] for field in successor):
        return False
    review_path = Path(str(gate.get("review_record") or "")).resolve()
    return (
        review_path.is_file()
        and isinstance(gate.get("review_record_sha256"), str)
        and gate["review_record_sha256"] == sha256_file(review_path)
    )


def _credential_status(path: Path) -> str:
    if not path.is_file():
        return "not_set"
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        return "unsafe_permissions"
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        return "unsafe_permissions"
    return "private_file"


def _load_key(path: Path) -> str:
    if _credential_status(path) != "private_file":
        raise ValueError("the Gemini credential is absent or not private")
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("the Gemini credential file must contain one nonempty line")
    return value


def _validate_payload(payload: dict[str, Any], config: dict[str, Any]) -> None:
    if set(payload) != {"systemInstruction", "contents", "generationConfig", "store"}:
        raise ValueError("the broker request has unsupported top-level fields")
    if payload.get("store") is not False:
        raise ValueError("the broker request must disable provider storage")
    generation = payload.get("generationConfig") or {}
    allowed_generation = {
        "candidateCount",
        "temperature",
        "responseMimeType",
        "responseJsonSchema",
        "maxOutputTokens",
        "thinkingConfig",
    }
    if not isinstance(generation, dict) or not set(generation) <= allowed_generation:
        raise ValueError("the broker generation config contains a prohibited feature")
    if generation.get("candidateCount") != 1:
        raise ValueError("the broker request must ask for one candidate")
    output = generation.get("maxOutputTokens")
    if isinstance(output, bool) or not isinstance(output, int) or output < 1:
        raise ValueError("the broker output-token limit is invalid")
    if output > int(config["maximum_output_tokens"]):
        raise ValueError("the broker output-token limit exceeds the price config")
    mime_type = generation.get("responseMimeType")
    if mime_type not in {"application/json", "text/x.enum"}:
        raise ValueError("the broker response MIME type is unsupported")
    thinking = generation.get("thinkingConfig")
    if "thinking_budget" in config and thinking != {
        "thinkingBudget": config["thinking_budget"]
    }:
        raise ValueError("the broker thinking control changed")
    if config.get("model") == "gemini-3.1-flash-lite" and thinking != {
        "thinkingLevel": config["thinking_level"]
    }:
        raise ValueError("the broker thinking control changed")
    if "thinking_levels" in config:
        # An evaluation model entry: the arm must be an official preset of the
        # model and the temperature must be the pinned API maximum.
        levels = config["thinking_levels"]
        if (
            not isinstance(thinking, dict)
            or set(thinking) != {"thinkingLevel"}
            or thinking["thinkingLevel"] not in levels
        ):
            raise ValueError("the evaluation thinking arm is not an official preset")
        if "temperature" not in generation or _money(
            generation["temperature"], "temperature"
        ) != _money(config["temperature"], "pinned temperature"):
            raise ValueError("the evaluation temperature is not the pinned maximum")
    for instruction in (payload.get("systemInstruction"), *payload.get("contents", [])):
        if not isinstance(instruction, dict):
            raise ValueError("the broker request content is invalid")
        parts = instruction.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("the broker request content lacks text parts")
        if any(not isinstance(part, dict) or set(part) != {"text"} for part in parts):
            raise ValueError("the broker request contains a non-text part")


def broker_request_key(
    *,
    model: str,
    run_id: str,
    phase: str = "live_test",
    stage: str,
    paper_id: str,
    family_id: str,
    source_version_id: str,
    payload: dict[str, Any],
    trial_id: str | None = None,
) -> str:
    """Bind one request key to its model, pipeline identity, and exact payload.

    An evaluation request also binds its run id and trial id, so two repeats
    of one identical stimulus never collide and are never treated as replays.
    """
    identity = {
        "model": model,
        "stage": stage,
        "paper_id": paper_id,
        "family_id": family_id,
        "source_version_id": source_version_id,
        "payload": payload,
    }
    if phase == "away_production":
        identity.update({"phase": phase, "run_id": run_id})
    if phase == EVALUATION_PHASE:
        if not trial_id:
            raise ValueError("an evaluation request key requires a trial id")
        identity.update({"phase": phase, "run_id": run_id, "trial_id": trial_id})
    return sha256_bytes(canonical_json(identity).encode())


class SharedGeminiBroker:
    """Meter every dataset-generation stage through one fail-closed ledger."""

    def __init__(
        self,
        *,
        policy_file: Path,
        price_config_file: Path,
        execution_gate_file: Path,
        ledger_file: Path,
        receipts_dir: Path,
        credential_file: Path,
        prior_construction_spend_usd: Decimal,
        transport: Any | None = None,
        config_transition_file: Path | None = None,
        evaluation_policy_file: Path | None = None,
        evaluation_price_config_file: Path | None = None,
        evaluation_gate_file: Path | None = None,
        evaluation_policy_transition_file: Path | None = None,
        concurrent_construction: bool = False,
    ) -> None:
        self.policy_file = policy_file.resolve()
        self.price_config_file = price_config_file.resolve()
        self.execution_gate_file = execution_gate_file.resolve()
        self.ledger_file = ledger_file.resolve()
        self.receipts_dir = receipts_dir.resolve()
        self.credential_file = credential_file.resolve()
        self.config_transition_file = (
            config_transition_file.resolve() if config_transition_file else None
        )
        self.policy = _validate_policy(self.policy_file)
        self.config = _config(self.price_config_file)
        self.active_price_config_sha256 = sha256_file(self.price_config_file)
        self.prior = _money(prior_construction_spend_usd, "prior construction spend")
        self.transport = transport
        self._config_transition_sha256: str | None = None
        self._config_transition_event_path: Path | None = None
        self._authorized_live_test_ceiling_usd: Decimal | None = None
        self._status_observer: Callable[[Path], None] | None = None
        self._stream_input_binding: dict[str, Any] | None = None
        # Admission concurrency (docs/SHARED_MODEL_BROKER.md, "Concurrent
        # evaluation requests"): one admission at a time per process, N calls
        # in flight, each guarded by its own in-flight lock file. The
        # evaluation phase always works this way. A construction run opts in
        # with ``concurrent_construction``, which the paper-concurrent producer
        # sets; without it a construction request keeps the historical shape of
        # one exclusive operation lock held for the whole call.
        self._admission_lock = threading.Lock()
        self.concurrent_construction = bool(concurrent_construction)
        # A concurrent broker defers the compacted snapshot and the status
        # file to its compactor thread; a broker that runs one operation at a
        # time writes them with the commit, which is what every reviewed
        # command sees. The streaming evaluator asks for the deferred shape
        # itself, because its own snapshot write would be 660 ms held under
        # the ledger lock that the producer waits for.
        self.deferred_snapshot = bool(concurrent_construction)
        self._pacing_state = threading.local()
        # The wait and the hold of every exclusive section, by lock handle, and
        # the whole-call lock of the sequential path by thread. The second one
        # makes ``_exclusive_operation`` a no-op where one lock is already held
        # for the whole call.
        self._operation_lock_waits: dict[Any, tuple[str, str, float, float]] = {}
        self._whole_call_operation: dict[int, Any] = {}
        # The immutable-event proof of each ledger row, by request key, with
        # the context it was proved under and when the last full pass ran.
        self._immutable_events_proved: dict[str, str] = {}
        self._immutable_events_context: tuple[Any, ...] | None = None
        self._immutable_events_proved_at: float | None = None
        self._ledger_evidence_proved: tuple[Any, ...] | None = None
        # The rows whose immutable-event proof is owed. ``None`` means the
        # requests map was replaced whole, by a reload of the snapshot or by a
        # reviewed repair, and every row is checked by its signature again.
        # A set is what the store reported moved, which is the same tracking
        # the money proof of the delta already trusts.
        self._immutable_events_pending: set[str] | None = None
        self._receipt_listing: tuple[tuple[int, int], list[str], float] | None = None
        # Everything derived from one receipts listing, thrown away with it.
        self._receipt_derived: dict[Any, Any] = {}
        # A terminal row's final receipt is immutable, so its custody is
        # proved once and never stated again.
        self._custody_proved: set[str] = set()
        # The parallel bookkeeping store: the snapshot, the journal beside it
        # and this process's view of both. Read "Parallel bookkeeping" in
        # docs/SHARED_MODEL_BROKER.md.
        self._store = LedgerStore(self.ledger_file)
        self._row_contributions: dict[str, dict[str, Any]] | None = None
        self._ledger_aggregate: dict[str, Any] | None = None
        self._aggregate_state_id: int | None = None
        self._compaction_thread: threading.Thread | None = None
        self._compaction_stop = threading.Event()
        self._compacted_seq = 0
        # One held shared ledger lock and one read of the ledger, per thread,
        # for the length of a session.
        self._ledger_session_state = threading.local()
        self._flush_state = threading.local()
        evaluation_files = (
            evaluation_policy_file,
            evaluation_price_config_file,
            evaluation_gate_file,
        )
        if any(evaluation_files) and not all(evaluation_files):
            raise ValueError(
                "benchmark evaluation needs its policy, price config and gate together"
            )
        self.evaluation_policy_file = (
            evaluation_policy_file.resolve() if evaluation_policy_file else None
        )
        self.evaluation_price_config_file = (
            evaluation_price_config_file.resolve()
            if evaluation_price_config_file
            else None
        )
        self.evaluation_gate_file = (
            evaluation_gate_file.resolve() if evaluation_gate_file else None
        )
        self.evaluation_policy_transition_file = (
            evaluation_policy_transition_file.resolve()
            if evaluation_policy_transition_file
            else None
        )
        self._evaluation_transition_sha256: str | None = None
        self.evaluation_policy: dict[str, Any] | None = None
        self.evaluation_config: dict[str, Any] | None = None
        self.active_evaluation_price_config_sha256: str | None = None
        self._evaluation_binding: dict[str, Any] | None = None
        if self.evaluation_policy_file is not None:
            self.evaluation_policy = _validate_evaluation_policy(
                self.evaluation_policy_file, self.policy
            )
            self.evaluation_config = _validate_evaluation_price_config(
                self.evaluation_price_config_file,
                self.config,  # type: ignore[arg-type]
            )
            self.active_evaluation_price_config_sha256 = sha256_file(
                self.evaluation_price_config_file  # type: ignore[arg-type]
            )
            gate = _validate_evaluation_gate(self.evaluation_gate_file)  # type: ignore[arg-type]
            self._validate_evaluation_gate_hashes(gate)
        self._initialize()
        if self.evaluation_policy is not None:
            self._authorize_evaluation_ceiling()

    def _validate_evaluation_gate_hashes(self, gate: dict[str, Any]) -> None:
        if gate["evaluation_policy_sha256"] != sha256_file(
            self.evaluation_policy_file  # type: ignore[arg-type]
        ):
            raise ValueError("the benchmark evaluation gate binds another policy")
        if gate["evaluation_price_config_sha256"] != (
            self.active_evaluation_price_config_sha256
        ):
            raise ValueError("the benchmark evaluation gate binds another price config")
        for model in gate["models"]:
            entry = (self.evaluation_config or {}).get("models", {}).get(model)
            if not isinstance(entry, dict):
                raise ValueError(f"the gate model has no price entry: {model}")
            for arm in gate["arms"]:
                if arm not in entry["thinking_levels"]:
                    raise ValueError(
                        f"the gate arm {arm} is not an official preset of {model}"
                    )

    # --- Reviewed evaluation ceiling ---------------------------------------

    def _evaluation_transition_events(self) -> list[tuple[Path, dict[str, Any]]]:
        """Read every applied evaluation-policy transition event, unordered."""
        events: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(
            self._receipt_paths(prefix="evaluation-policy-transition-", suffix=".json")
        ):
            event = _read(path)
            authorization = event.get("authorization")
            if (
                event.get("schema") != EVALUATION_POLICY_TRANSITION_EVENT_SCHEMA
                or not isinstance(authorization, dict)
                or set(authorization) != EVALUATION_POLICY_TRANSITION_FIELDS
            ):
                raise ValueError(EVALUATION_TRANSITION_CHANGED_REASON)
            digest = sha256_bytes(canonical_json(authorization).encode())
            if (
                event.get("transition_authorization_sha256") != digest
                or path.name != f"evaluation-policy-transition-{digest}.json"
            ):
                raise ValueError(EVALUATION_TRANSITION_CHANGED_REASON)
            applied = str(event.get("applied_at_utc") or "")
            try:
                stamp = datetime.fromisoformat(applied.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError(EVALUATION_TRANSITION_CHANGED_REASON) from error
            if stamp.tzinfo is None:
                raise ValueError(EVALUATION_TRANSITION_CHANGED_REASON)
            events.append((path, event))
        return events

    def _evaluation_transition_chain(self) -> list[dict[str, Any]]:
        """Order the applied events by their predecessor links.

        Each event names the authorization hash of the event before it, or
        ``None`` when it is the first. A missing predecessor, a fork or an
        extra event is an error, so a replaced or added event cannot widen
        the ceiling.
        """
        by_predecessor: dict[str | None, dict[str, Any]] = {}
        for _, event in self._evaluation_transition_events():
            key = event["authorization"]["from_evaluation_transition_sha256"]
            if key in by_predecessor:
                raise ValueError(
                    "two evaluation policy transitions share one predecessor"
                )
            by_predecessor[key] = event
        chain: list[dict[str, Any]] = []
        predecessor: str | None = None
        while predecessor in by_predecessor:
            event = by_predecessor.pop(predecessor)
            chain.append(event)
            predecessor = event["transition_authorization_sha256"]
        if by_predecessor:
            raise ValueError(
                "an evaluation policy transition has no applied predecessor"
            )
        return chain

    @property
    def evaluation_transition_sha256(self) -> str | None:
        """The applied evaluation-policy transition this broker runs under."""
        return self._evaluation_transition_sha256

    def authorized_evaluation_ceiling_usd(self) -> Decimal:
        """The evaluation ceiling the reviewed transition chain authorizes."""
        chain = self._evaluation_transition_chain()
        if not chain:
            return EVALUATION_BASELINE_CEILING_USD
        change = chain[-1]["authorization"]["changed_policy_fields"]
        return _money(change["evaluation_ceiling_usd"]["to"], "authorized ceiling")

    def _active_evaluation_transition_sha256(self) -> str | None:
        chain = self._evaluation_transition_chain()
        return chain[-1]["transition_authorization_sha256"] if chain else None

    def _authorize_evaluation_ceiling(self) -> None:
        """Bind the active evaluation ceiling to the reviewed chain.

        A ceiling at or below the authorized one needs nothing: a smaller
        ceiling only tightens the control. A larger one needs
        ``evaluation_policy_transition_file``, whose change set must be one of
        ``EVALUATION_CEILING_CHANGES``. Applying it writes one immutable
        event, and every later start reads that event instead of the file.
        """
        policy = self.evaluation_policy or {}
        active = _money(policy["evaluation_ceiling_usd"], "evaluation ceiling")
        authorized = self.authorized_evaluation_ceiling_usd()
        if active <= authorized:
            self._evaluation_transition_sha256 = (
                self._active_evaluation_transition_sha256()
            )
            return
        if self.evaluation_policy_transition_file is None:
            raise ValueError(
                "the active evaluation ceiling requires a reviewed transition"
            )
        authorization = _read(self.evaluation_policy_transition_file)
        self._validate_evaluation_policy_transition(authorization)
        digest = sha256_bytes(canonical_json(authorization).encode())
        event_path = self.receipts_dir / f"evaluation-policy-transition-{digest}.json"
        if not event_path.exists():
            atomic_json(
                event_path,
                {
                    "schema": EVALUATION_POLICY_TRANSITION_EVENT_SCHEMA,
                    "authorization": authorization,
                    "transition_authorization_sha256": digest,
                    "applied_at_utc": _now(),
                },
                immutable=True,
            )
        self._evaluation_transition_sha256 = digest
        if self.authorized_evaluation_ceiling_usd() < active:
            raise ValueError(EVALUATION_TRANSITION_CHANGED_REASON)

    def _validate_evaluation_policy_transition(
        self, authorization: dict[str, Any]
    ) -> None:
        """Validate one evaluation-policy transition before it is applied."""
        if (
            not isinstance(authorization, dict)
            or set(authorization) != EVALUATION_POLICY_TRANSITION_FIELDS
            or authorization.get("schema") != EVALUATION_POLICY_TRANSITION_SCHEMA
        ):
            raise ValueError("the evaluation policy transition file is invalid")
        change = authorization["changed_policy_fields"]
        if change not in EVALUATION_CEILING_CHANGES:
            raise ValueError("the evaluation policy change set is not authorized")
        if Path(str(authorization["ledger_file"])).resolve() != self.ledger_file:
            raise ValueError("the evaluation policy transition names another ledger")
        if authorization["expected_ledger_sha256"] != sha256_file(self.ledger_file):
            raise ValueError("the evaluation policy transition ledger snapshot changed")
        if authorization["evaluation_gate_sha256"] != sha256_file(
            self.evaluation_gate_file  # type: ignore[arg-type]
        ):
            raise ValueError("the evaluation policy transition binds another gate")
        source_path = Path(str(authorization["from_policy_file"]))
        if not source_path.is_file():
            raise ValueError("the evaluation policy transition source is absent")
        if sha256_file(source_path) != authorization["from_policy_sha256"]:
            raise ValueError("the evaluation policy transition source changed")
        if authorization["to_policy_sha256"] != sha256_file(
            self.evaluation_policy_file  # type: ignore[arg-type]
        ):
            raise ValueError("the evaluation policy transition target changed")
        source = _read(source_path)
        target = _read(self.evaluation_policy_file)  # type: ignore[arg-type]
        for field, values in change.items():
            if str(source.get(field)) != str(values["from"]):
                raise ValueError(
                    f"the evaluation policy source {field} is not the authorized value"
                )
            if str(target.get(field)) != str(values["to"]):
                raise ValueError(
                    f"the evaluation policy target {field} is not the authorized value"
                )
        # Only the authorized fields may differ, and `policy_id` and `purpose`
        # name the new revision. Every control field must stay identical.
        described = set(change) | {"policy_id", "purpose"}
        if {k: v for k, v in source.items() if k not in described} != {
            k: v for k, v in target.items() if k not in described
        }:
            raise ValueError("the evaluation policy transition changes another field")
        if (
            _money(
                change["evaluation_ceiling_usd"]["from"],
                "authorized predecessor ceiling",
            )
            != self.authorized_evaluation_ceiling_usd()
        ):
            raise ValueError(
                "the evaluation policy transition does not start at the active ceiling"
            )
        if (
            authorization["from_evaluation_transition_sha256"]
            != self._active_evaluation_transition_sha256()
        ):
            raise ValueError(
                "the evaluation policy transition names another predecessor"
            )
        if not str(authorization["integrated_code_commit"] or "").strip():
            raise ValueError("the evaluation policy transition lacks its code commit")
        if not str(authorization["reason"] or "").strip():
            raise ValueError("the evaluation policy transition lacks its reason")
        try:
            stamp = datetime.fromisoformat(
                str(authorization["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(
                "the evaluation policy transition authorization time is invalid"
            ) from error
        if stamp.tzinfo is None:
            raise ValueError(
                "the evaluation policy transition authorization time is invalid"
            )
        review_path = Path(str(authorization["review_record"]))
        if not review_path.is_file():
            raise ValueError("the evaluation policy transition review record is absent")
        if sha256_file(review_path) != authorization["review_record_sha256"]:
            raise ValueError("the evaluation policy transition review record changed")
        with self._ledger_lock():
            ledger = self._validated_ledger()
        if ledger["halted"] or self._phase_halted(ledger, EVALUATION_PHASE):
            raise ValueError("the ledger is halted")
        evaluation = self._evaluation_totals(ledger)
        if evaluation["inflight"]:
            raise ValueError("an evaluation request is in flight")
        if evaluation["ambiguous_usd"] > 0:
            raise ValueError("an evaluation request has an ambiguous charge")
        if evaluation["used_usd"] > _money(
            change["evaluation_ceiling_usd"]["to"], "authorized ceiling"
        ):
            raise ValueError("the evaluation spend already exceeds the new ceiling")

    def evaluation_enabled(self) -> bool:
        return self.evaluation_policy is not None

    def _require_evaluation(self) -> None:
        if not self.evaluation_enabled():
            raise ValueError(
                "the broker has no benchmark evaluation policy, price config and gate"
            )

    def bind_evaluation(
        self,
        *,
        eval_set_manifest_file: Path,
        eval_set_id: str,
        run_id: str,
        models: list[str],
        arms: list[str],
        repeats: int,
        prompt_version: str,
        prompt_sha256: str,
        abstention_option_text: str,
        decoding: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind one reviewed evaluation run to the gate before any paid call."""
        self._require_evaluation()
        gate = _validate_evaluation_gate(self.evaluation_gate_file)  # type: ignore[arg-type]
        self._validate_evaluation_gate_hashes(gate)
        binding = {
            "eval_set_manifest_file": eval_set_manifest_file.resolve(),
            "eval_set_id": eval_set_id,
            "run_id": run_id,
            "models": list(models),
            "arms": list(arms),
            "repeats": repeats,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
            "abstention_option_text": abstention_option_text,
            "decoding": decoding,
        }
        self._validate_evaluation_binding(gate, binding, request_run_id=run_id)
        self._evaluation_binding = binding
        return binding

    @staticmethod
    def _validate_evaluation_binding(
        gate: dict[str, Any],
        binding: dict[str, Any] | None,
        *,
        request_run_id: str | None = None,
    ) -> None:
        if binding is None:
            raise ValueError("the benchmark evaluation run is not bound")
        manifest = binding["eval_set_manifest_file"]
        if (
            not manifest.is_file()
            or sha256_file(manifest) != (gate["eval_set_manifest_sha256"])
        ):
            raise ValueError("the reviewed evaluation set manifest changed")
        if binding["eval_set_id"] != gate["eval_set_id"]:
            raise ValueError("the reviewed evaluation set identity changed")
        if binding["run_id"] != gate["authorized_run_id"] or (
            request_run_id is not None and request_run_id != gate["authorized_run_id"]
        ):
            raise ValueError("the reviewed evaluation run identity changed")
        if (
            binding["prompt_version"] != gate["prompt_version"]
            or binding["prompt_sha256"] != gate["prompt_sha256"]
            or binding["abstention_option_text"] != gate["abstention_option_text"]
        ):
            raise ValueError("the reviewed evaluation prompt changed")
        if not set(binding["models"]) <= set(gate["models"]) or not binding["models"]:
            raise ValueError("the reviewed evaluation model list changed")
        if not set(binding["arms"]) <= set(gate["arms"]) or not binding["arms"]:
            raise ValueError("the reviewed evaluation thinking arms changed")
        repeats = binding["repeats"]
        if (
            isinstance(repeats, bool)
            or not isinstance(repeats, int)
            or not 1 <= repeats <= int(gate["repeats_maximum"])
        ):
            raise ValueError("the reviewed evaluation repeat count changed")
        if binding["decoding"] != gate["decoding"]:
            raise ValueError("the reviewed evaluation decoding settings changed")

    def config_for_stage(self, stage: str) -> dict[str, Any]:
        """Return the registered model and price values for one stage."""
        if is_evaluation_stage(stage):
            if self.evaluation_config is None:
                raise ValueError(
                    "an evaluation stage needs the benchmark evaluation price config"
                )
            return evaluation_model_config(self.evaluation_config, stage)
        return model_config_for_stage(self.config, stage)

    def _timeout_for_stage(self, stage: str) -> int:
        if is_evaluation_stage(stage):
            entry = self.config_for_stage(stage)
            if "call_timeout_seconds" not in entry:
                return DEFAULT_CALL_TIMEOUT_SECONDS
            return int(entry["call_timeout_seconds"])
        return call_timeout_seconds(self.config, stage)

    @property
    def _pacing_phase(self) -> str:
        return getattr(self._pacing_state, "phase", "live_test")

    @_pacing_phase.setter
    def _pacing_phase(self, phase: str) -> None:
        self._pacing_state.phase = phase

    @property
    def _lock_file(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.lock")

    @property
    def _inflight_lock_dir(self) -> Path:
        return self.receipts_dir / ".inflight"

    def _inflight_lock_path(self, request_key: str) -> Path:
        return self._inflight_lock_dir / f"{request_key}.lock"

    def _hold_inflight(self, request_key: str) -> Any:
        """Hold the in-flight lock of one submitted request for its live call.

        The lock is an ``flock`` on a per-request file. Orphan recovery skips
        a submitted request whose lock is held, so N evaluation calls of one
        run can be in flight at once. A crashed holder releases the lock, and
        the next recovery settles the request as before.
        """
        self._inflight_lock_dir.mkdir(parents=True, exist_ok=True)
        handle = self._inflight_lock_path(request_key).open("a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def _inflight_held(self, request_key: str) -> bool:
        path = self._inflight_lock_path(request_key)
        if not path.exists():
            return False
        with path.open("a+") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
        return False

    @staticmethod
    def _evaluation_recent_submission_times(
        ledger: dict[str, Any], cutoff: datetime
    ) -> list[datetime]:
        """Return the evaluation submissions inside the current minute window.

        The evaluation phase keeps its own window, read from the submission
        times of its requests, so it never consumes the construction window
        ``recent_submission_times_utc`` and the construction pace stays exact.
        """
        times = []
        for request in ledger["requests"].values():
            if request.get("phase") != EVALUATION_PHASE:
                continue
            submitted = request.get("submitted_at_utc")
            if not submitted:
                continue
            value = datetime.fromisoformat(str(submitted).replace("Z", "+00:00"))
            if value > cutoff:
                times.append(value)
        return sorted(times)

    @property
    def _operation_lock_file(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.operation.lock")

    def _operation_lock_line(self, event: str, fields: dict[str, Any]) -> None:
        """Write one measurement line about the exclusive operation lock.

        The line goes to stderr, because stdout of the producer is one JSON
        result. A launcher keeps both in its log, so the wait and the hold of
        every exclusive section are measurable from that log alone.
        """
        parts = " ".join(f"{name}={value}" for name, value in fields.items())
        try:
            print(
                f"[operation-lock] {_now()} pid={os.getpid()} "
                f"thread={threading.current_thread().name} {event} {parts}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            # The measurement is never authoritative and never stops a request.
            pass

    def _acquire_operation_lock(self, section: str, request_key: str) -> Any:
        """Queue for the exclusive operation lock and return the held handle.

        A concurrent request waits for the whole queue in front of it. The wait
        has a sanity ceiling in minutes; a ceiling that is reached retries the
        same request key instead of faulting the paper, because the request has
        reserved nothing and describes no fault of its own. Only a lock still
        held after every round raises :class:`BrokerOperationBusyError`, which
        the producer contains against one paper as it always has.
        """
        queued = self.concurrent_construction
        wait_seconds = (
            OPERATION_LOCK_QUEUE_CEILING_SECONDS
            if queued
            else OPERATION_LOCK_WAIT_SECONDS
        )
        rounds = OPERATION_LOCK_QUEUE_ROUNDS if queued else 1
        started = time.monotonic()
        for attempt in range(1, rounds + 1):
            try:
                handle = hold_operation_lock(
                    self._operation_lock_file,
                    wait_seconds=wait_seconds,
                    busy_error=BrokerOperationBusyError,
                    # Every broker polls at the short interval. A hold lasts
                    # milliseconds, so a one-second poll of the sequential
                    # path paid a whole second per collision: the
                    # evaluation pacing test measured 1.5 s for 1.2 s of
                    # calls under load on 2026-09-17. The bound and the
                    # rounds of each path are unchanged.
                    poll_seconds=OPERATION_LOCK_CONCURRENT_WAIT_INTERVAL_SECONDS,
                    heartbeat_seconds=OPERATION_LOCK_HEARTBEAT_SECONDS,
                    heartbeat=lambda waited, section=section: self._operation_lock_line(
                        "waiting",
                        {
                            "section": section,
                            "request": request_key[:16],
                            "waited_s": f"{waited:.1f}",
                        },
                    ),
                )
            except BrokerOperationBusyError:
                if attempt == rounds:
                    self._operation_lock_line(
                        "gave_up",
                        {
                            "section": section,
                            "request": request_key[:16],
                            "waited_s": f"{time.monotonic() - started:.1f}",
                            "rounds": rounds,
                        },
                    )
                    raise
                self._operation_lock_line(
                    "ceiling_retry",
                    {
                        "section": section,
                        "request": request_key[:16],
                        "waited_s": f"{time.monotonic() - started:.1f}",
                        "round": attempt,
                    },
                )
                continue
            self._operation_lock_waits[handle] = (
                section,
                request_key,
                time.monotonic() - started,
                time.monotonic(),
            )
            return handle
        raise BrokerOperationBusyError(OPERATION_LOCK_BUSY_REASON)

    def _release_operation_lock(self, handle: Any) -> None:
        record = self._operation_lock_waits.pop(handle, None)
        handle.close()
        if record is None:
            return
        section, request_key, waited, held_from = record
        held = time.monotonic() - held_from
        if max(waited, held) >= OPERATION_LOCK_LOG_THRESHOLD_SECONDS:
            self._operation_lock_line(
                "section",
                {
                    "section": section,
                    "request": request_key[:16],
                    "waited_s": f"{waited:.2f}",
                    "held_s": f"{held:.2f}",
                },
            )

    @contextlib.contextmanager
    def _ledger_lock(self) -> Iterator[None]:
        """Hold the shared ledger lock, then make the journal durable.

        The flush is deliberately outside the lock. An appended record is
        already visible to every reader of the journal the moment it is
        written; what the flush adds is durability against power loss, and the
        money rule needs that before the provider call, not before the lock is
        released. Outside the lock one thread's flush covers every record its
        peers appended, so a wave of sixteen concurrent calls pays one
        `fsync`, not sixteen. A durable write on this data disk costs 150 to
        470 ms, which is the whole reason:
        `research/arctic-ledger-parallel-r1/report.md`.
        """
        try:
            with self._lock_file.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            if not getattr(self._flush_state, "deferred", False):
                self._store.flush()

    @contextlib.contextmanager
    def _deferred_flush(self) -> Iterator[None]:
        """Hold the journal's flush until the admission is over.

        Admission is serialised, so a flush inside it is a flush nobody
        shares. The free count event is not money and owes no flush at all,
        and the reservation owes one before the provider call, which is after
        the admission lock is released. ``execute`` flushes there, where the
        reservations of sixteen concurrent calls meet in one ``fsync``.
        """
        self._flush_state.deferred = True
        try:
            yield
        finally:
            self._flush_state.deferred = False

    @contextlib.contextmanager
    def _ledger_session(self) -> Iterator[None]:
        """Hold the shared ledger lock across several ledger operations.

        Three operations register one counted request, and each of them took
        the shared ledger lock on its own. That lock is shared with the
        benchmark evaluator, whose own hold was measured at a median of 2.35
        seconds on 2026-09-17, so three acquisitions cost about seven seconds
        of waiting for one paid call. Inside a session the lock is taken once
        and the validated ledger is read once: no other writer can change the
        file while it is held, so the second read would return the same bytes.

        Every ledger operation inside a session must go through
        :meth:`_locked_ledger`. ``flock`` is held by an open file description,
        so a second ``open`` of the lock file inside a session deadlocks.
        """
        state = self._ledger_session_state
        if getattr(state, "depth", 0):
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
            return
        with self._ledger_lock():
            state.depth = 1
            state.ledger = None
            try:
                yield
            finally:
                state.depth = 0
                state.ledger = None

    @contextlib.contextmanager
    def _locked_ledger(self) -> Iterator[dict[str, Any]]:
        """Yield the validated ledger under the shared ledger lock.

        Outside a session this is the historical shape: take the lock, read
        and validate, act, release. Inside one, the lock is already held and
        the ledger already read, and a commit writes the very object that is
        held, so the session's copy stays the file's content.
        """
        state = self._ledger_session_state
        if getattr(state, "depth", 0):
            if state.ledger is None:
                state.ledger = self._validated_ledger()
            yield state.ledger
            return
        with self._ledger_lock():
            yield self._validated_ledger()

    @contextlib.contextmanager
    def _exclusive_operation(self, section: str, request_key: str) -> Iterator[None]:
        """Hold the exclusive operation lock for one short ledger mutation.

        The sequential path holds one lock for the whole call, so this is a
        no-op there: ``flock`` of the same file from a second descriptor of one
        process blocks against itself. The concurrent path holds nothing
        between its mutations, so every section takes the lock on its own and
        keeps only the mutation inside it.
        """
        if self._whole_call_operation.get(threading.get_ident()) is not None:
            yield
            return
        handle = self._acquire_operation_lock(section, request_key)
        try:
            yield
        finally:
            self._release_operation_lock(handle)

    @property
    def _identity_file(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.identity.json")

    @property
    def _status_file(self) -> Path:
        return self.ledger_file.with_name(f"{self.ledger_file.stem}.status.json")

    @property
    def _integrity_file(self) -> Path:
        return self.ledger_file.with_name(
            f".{self.ledger_file.name}.integrity-halt.json"
        )

    @staticmethod
    def _request_event_stem(request_key: str, request: dict[str, Any]) -> str:
        count_retry_round = request.get("count_retry_round")
        if count_retry_round:
            return _count_event_stem(request_key, int(count_retry_round))
        if request.get("resumed_from_not_submitted_sha256") is not None:
            return f"{request_key}.resume-{request['config_transition_sha256']}"
        return request_key

    @classmethod
    def validate_no_replay_liabilities(
        cls,
        *,
        ledger: dict[str, Any],
        receipts_dir: Path,
    ) -> dict[str, dict[str, Any]]:
        """Validate reviewed no-replay holds without opening or changing the ledger.

        Batch submission uses this read-only seam so it can share the exact
        continuation evidence and custody checks used by the live broker.
        Constructing a broker here would require live policy and credential
        inputs and could write an integrity halt on malformed data.
        """
        validator = cls.__new__(cls)
        validator.receipts_dir = receipts_dir.resolve()
        validator._receipt_listing = None
        validator._receipt_derived = {}
        validator._custody_proved = set()
        ambiguous = validator._ambiguous_continuation_events(ledger)
        orphaned = validator._orphaned_continuation_events(ledger)
        overlap = set(ambiguous) & set(orphaned)
        if overlap:
            raise ValueError("a no-replay continuation request changed")
        return {**ambiguous, **orphaned}

    def _ledger_identity(self) -> dict[str, Any]:
        return {
            "schema": "shared-paid-call-ledger-identity-v1",
            "ledger_file": str(self.ledger_file),
            "policy_sha256": sha256_file(self.policy_file),
            "price_config_sha256": self.active_price_config_sha256,
            "prior_construction_spend_usd": str(self.prior),
        }

    def _validate_initial_identity(
        self, identity: dict[str, Any], ledger: dict[str, Any]
    ) -> None:
        required = {
            "schema",
            "ledger_file",
            "policy_sha256",
            "price_config_sha256",
            "prior_construction_spend_usd",
        }
        if not isinstance(identity, dict) or set(identity) != required:
            raise ValueError("the shared paid-call ledger identity record changed")
        if (
            identity["schema"] != "shared-paid-call-ledger-identity-v1"
            or identity["ledger_file"] != str(self.ledger_file)
            or _money(identity["prior_construction_spend_usd"], "identity prior")
            != self.prior
        ):
            raise ValueError("the shared paid-call ledger identity record changed")
        if (
            ledger.get("schema") != "shared-paid-call-ledger-v1"
            or ledger.get("policy_sha256") != identity["policy_sha256"]
            or ledger.get("price_config_sha256") != identity["price_config_sha256"]
            or _money(ledger.get("prior_construction_spend_usd"), "prior") != self.prior
        ):
            raise ValueError("the shared paid-call ledger identity changed")

    @staticmethod
    def _transition_fields(authorization: dict[str, Any]) -> set[str]:
        if authorization.get("schema") == "shared-paid-call-config-transition-v1":
            return CONFIG_TRANSITION_V1_FIELDS
        if authorization.get("schema") == "shared-paid-call-config-transition-v2":
            return CONFIG_TRANSITION_V2_FIELDS
        if authorization.get("schema") == "shared-paid-call-config-transition-v3":
            return CONFIG_TRANSITION_V3_FIELDS
        return set()

    @staticmethod
    def _transition_policy_hashes(
        authorization: dict[str, Any], identity: dict[str, Any]
    ) -> tuple[str, str]:
        if authorization.get("schema") in {
            "shared-paid-call-config-transition-v2",
            "shared-paid-call-config-transition-v3",
        }:
            return (
                str(authorization.get("from_policy_sha256") or ""),
                str(authorization.get("to_policy_sha256") or ""),
            )
        initial = str(identity["policy_sha256"])
        return initial, initial

    def _transition_pairs(
        self, authorization: dict[str, Any], identity: dict[str, Any]
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        from_policy, to_policy = self._transition_policy_hashes(authorization, identity)
        return (
            (str(authorization.get("from_price_config_sha256") or ""), from_policy),
            (str(authorization.get("to_price_config_sha256") or ""), to_policy),
        )

    def _apply_transition_controls(self, authorization: dict[str, Any]) -> None:
        if (
            authorization.get("schema") == "shared-paid-call-config-transition-v2"
            and authorization.get("changed_policy_fields") not in CEILING_CHANGES
        ):
            self._authorized_live_test_ceiling_usd = _money(
                authorization["maximum_authorized_cumulative_tranche_usd"],
                "transition tranche",
                positive=True,
            )

    @staticmethod
    def _is_ceiling_extension(authorization: dict[str, Any]) -> bool:
        return (
            authorization.get("schema") == "shared-paid-call-config-transition-v2"
            and authorization.get("changed_policy_fields") == CEILING_EXTENSION_CHANGE
        )

    def _read_transition_event(self, path: Path) -> dict[str, Any]:
        event = _read(path)
        if not isinstance(event, dict) or set(event) != {
            "schema",
            "authorization",
            "transition_authorization_sha256",
            "applied_at_utc",
        }:
            raise ValueError("the applied price configuration transition changed")
        authorization = event["authorization"]
        expected_fields = (
            self._transition_fields(authorization)
            if isinstance(authorization, dict)
            else set()
        )
        if (
            event["schema"] != "shared-paid-call-config-transition-event-v1"
            or not isinstance(authorization, dict)
            or set(authorization) != expected_fields
        ):
            raise ValueError("the applied price configuration transition changed")
        authorization_hash = sha256_bytes(canonical_json(authorization).encode())
        if (
            event["transition_authorization_sha256"] != authorization_hash
            or path.name != f"config-transition-{authorization_hash}.json"
        ):
            raise ValueError("the applied price configuration transition changed")
        try:
            applied = datetime.fromisoformat(
                str(event["applied_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(
                "the applied price configuration transition changed"
            ) from error
        if applied.tzinfo is None:
            raise ValueError("the applied price configuration transition changed")
        return event

    def _validate_transition_durable_bindings(
        self, authorization: dict[str, Any]
    ) -> None:
        gate_phase = _read(self.execution_gate_file).get("allowed_phase")
        if gate_phase not in CONSTRUCTION_PHASES:
            raise ValueError("the configuration transition gate phase is invalid")
        gate = _validate_gate(self.execution_gate_file, gate_phase)
        if authorization.get("changed_policy_fields") in (
            UNBOUNDED_COUNT_CHANGE,
            LIVE_TEST_BUDGET_EXTENSION_CHANGE,
            *CEILING_CHANGES,
        ) or self._is_ceiling_extension(authorization):
            self._validate_stream_input_gate(gate)
        direct_gate_binding = (
            authorization["execution_gate_sha256"]
            == sha256_file(self.execution_gate_file)
            and authorization["integrated_code_commit"]
            == gate["integrated_code_commit"]
            and authorization["review_record"] == gate["review_record"]
            and authorization["review_record_sha256"]
            == gate.get("review_record_sha256")
        )
        if not direct_gate_binding and not _gate_succeeds_transition_review(
            gate, authorization
        ):
            raise ValueError("the configuration transition review changed")
        review_path = Path(authorization["review_record"]).resolve()
        if not review_path.is_file() or authorization[
            "review_record_sha256"
        ] != sha256_file(review_path):
            raise ValueError("the configuration transition review is invalid")

    @staticmethod
    def _only_evaluation_activity_since(
        ledger: dict[str, Any], applied_at_utc: str
    ) -> bool:
        """Return whether every request touched after ``applied_at_utc`` is evaluation.

        An applied transition waits for its first construction request. The
        evaluation phase runs on the same ledger under its own ceiling, so its
        requests may land in between; a construction request may not.
        """
        applied = datetime.fromisoformat(str(applied_at_utc).replace("Z", "+00:00"))
        for request in ledger["requests"].values():
            times = [
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                for value in (
                    request.get("submitted_at_utc"),
                    request.get("completed_at_utc"),
                )
                if value
            ]
            # A record without a timestamp predates the timestamps (one legacy
            # count-stage record carries none); the snapshot hash covered it.
            # Timestamps carry seconds; a request from the second of the
            # application counts as later, which errs toward a refused start.
            if not times or max(times) < applied:
                continue
            # A request that never reached its reservation carries no phase:
            # the row is created before the phase is recorded, and a refusal
            # before the reserve leaves it unset. The stage family is the
            # authority either way, and ``execute`` refuses a request whose
            # phase and stage family disagree, so a phase-less evaluation row
            # is still evaluation activity. Reading it as construction stopped
            # every start after a transition (2026-09-16 23:04 UTC).
            if request.get("phase") != EVALUATION_PHASE and not is_evaluation_stage(
                request.get("stage")
            ):
                return False
        return True

    def _validate_transition_authorization(
        self,
        authorization: dict[str, Any],
        *,
        identity: dict[str, Any],
        ledger: dict[str, Any],
        applied_at_utc: str | None = None,
    ) -> None:
        expected_fields = (
            self._transition_fields(authorization)
            if isinstance(authorization, dict)
            else set()
        )
        if not expected_fields or set(authorization) != expected_fields:
            raise ValueError("the configuration transition fields changed")
        initial_pair = (
            identity["price_config_sha256"],
            identity["policy_sha256"],
        )
        active_pair = (
            self.active_price_config_sha256,
            sha256_file(self.policy_file),
        )
        from_pair, to_pair = self._transition_pairs(authorization, identity)
        if (
            authorization["ledger_file"] != str(self.ledger_file)
            or to_pair != active_pair
        ):
            raise ValueError("the configuration transition identity changed")
        if authorization["schema"] == "shared-paid-call-config-transition-v1":
            if from_pair != initial_pair:
                raise ValueError("the configuration transition identity changed")
        elif authorization["schema"] == "shared-paid-call-config-transition-v3":
            source_policy = Path(authorization["from_policy_file"]).resolve()
            predecessor = authorization["from_config_transition_sha256"]
            if (
                from_pair[0] == to_pair[0]
                or from_pair[1] != to_pair[1]
                or not source_policy.is_file()
                or sha256_file(source_policy) != from_pair[1]
                or _read(source_policy) != _read(self.policy_file)
                or not isinstance(predecessor, str)
            ):
                raise ValueError("the price configuration transition identity changed")
            matching_predecessors = []
            for path in self._receipt_paths(
                prefix="config-transition-", suffix=".json"
            ):
                if sha256_file(path) != predecessor:
                    continue
                event = self._read_transition_event(path)
                if (
                    self._transition_pairs(event["authorization"], identity)[1]
                    == from_pair
                ):
                    matching_predecessors.append(path)
            if len(matching_predecessors) != 1:
                raise ValueError("the price configuration predecessor changed")
        else:
            changed_policy_fields = authorization["changed_policy_fields"]
            ceiling_extension = self._is_ceiling_extension(authorization)
            if (
                not ceiling_extension
                and changed_policy_fields not in POLICY_TRANSITION_CHANGES
            ):
                raise ValueError("the policy transition change set changed")
            tranche = _money(
                authorization["maximum_authorized_cumulative_tranche_usd"],
                "transition tranche",
                positive=True,
            )
            if from_pair[0] != to_pair[0]:
                raise ValueError("the policy transition identity changed")
            source_policy = Path(authorization["from_policy_file"]).resolve()
            if (
                not source_policy.is_file()
                or sha256_file(source_policy) != from_pair[1]
            ):
                raise ValueError("the policy transition source changed")
            source_value = _read(source_policy)
            active_value = _read(self.policy_file)
            predecessor = authorization["from_config_transition_sha256"]
            if ceiling_extension:
                if (
                    from_pair != to_pair
                    or tranche != Decimal("10")
                    or active_value != source_value
                    or predecessor is None
                ):
                    raise ValueError("the policy transition identity changed")
                matching_predecessors = []
                for path in self._receipt_paths(
                    prefix="config-transition-", suffix=".json"
                ):
                    if sha256_file(path) != predecessor:
                        continue
                    event = self._read_transition_event(path)
                    prior = event["authorization"]
                    if (
                        self._transition_pairs(prior, identity)[1] == from_pair
                        and prior.get("changed_policy_fields") == UNBOUNDED_COUNT_CHANGE
                        and _money(
                            prior.get("maximum_authorized_cumulative_tranche_usd"),
                            "predecessor tranche",
                            positive=True,
                        )
                        == Decimal("5")
                    ):
                        matching_predecessors.append(path)
                if len(matching_predecessors) != 1:
                    raise ValueError("the policy transition predecessor changed")
            else:
                if changed_policy_fields == LIVE_TEST_BUDGET_EXTENSION_CHANGE:
                    expected_tranche = Decimal("20")
                elif changed_policy_fields == PRODUCTION_BUDGET_EXTENSION_CHANGE:
                    expected_tranche = Decimal("61.614496")
                elif changed_policy_fields == CHAPTER2_BUDGET_EXTENSION_CHANGE:
                    expected_tranche = CHAPTER2_CUMULATIVE_CEILING_USD
                elif changed_policy_fields == CHAPTER3_BUDGET_CHANGE:
                    expected_tranche = CHAPTER3_CUMULATIVE_CEILING_USD
                elif changed_policy_fields in (
                    CHAPTER3_EXPANSION_CHANGE,
                    # The request-rate transitions move no money, so each
                    # names the ceiling the expansion already authorized. The
                    # parallel one was refused at 04:53 UTC on 2026-09-17 for
                    # the USD 5 default until it was named here.
                    CHAPTER3_CONCURRENCY_CHANGE,
                    CHAPTER3_PARALLEL_CHANGE,
                    CHAPTER3_SCALE_CHANGE,
                ):
                    expected_tranche = CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD
                else:
                    expected_tranche = Decimal("5")
                if from_pair[1] == to_pair[1] or tranche != expected_tranche:
                    raise ValueError("the policy transition identity changed")
                expected_value = dict(source_value)
                for field, limits in changed_policy_fields.items():
                    if source_value.get(field) != limits["from"]:
                        raise ValueError("the policy transition source limit changed")
                    expected_value[field] = limits["to"]
                if active_value != expected_value:
                    raise ValueError(
                        "the policy transition changes more than one field"
                    )
                if from_pair == initial_pair:
                    if predecessor is not None:
                        raise ValueError("the policy transition predecessor changed")
                else:
                    matching_predecessors = []
                    for path in self._receipt_paths(
                        prefix="config-transition-", suffix=".json"
                    ):
                        if sha256_file(path) != predecessor:
                            continue
                        event = self._read_transition_event(path)
                        prior = event["authorization"]
                        valid_budget_predecessor = (
                            changed_policy_fields != LIVE_TEST_BUDGET_EXTENSION_CHANGE
                            or (
                                self._is_ceiling_extension(prior)
                                and _money(
                                    prior.get(
                                        "maximum_authorized_cumulative_tranche_usd"
                                    ),
                                    "predecessor tranche",
                                    positive=True,
                                )
                                == Decimal("10")
                            )
                        )
                        if (
                            self._transition_pairs(prior, identity)[1] == from_pair
                            and valid_budget_predecessor
                        ):
                            matching_predecessors.append(path)
                    if len(matching_predecessors) != 1:
                        raise ValueError("the policy transition predecessor changed")
        if authorization["expected_ledger_sha256"] != sha256_file(self.ledger_file):
            # Before its first construction request, an applied transition is
            # validated again on every start. Evaluation requests made since
            # the application are the one change the ledger may carry: the
            # evaluation phase never stops construction (2026-09-16 11:29 UTC,
            # twelve evaluation calls between the chapter 3 expansion event
            # and the relaunch of the producer).
            if applied_at_utc is None or not self._only_evaluation_activity_since(
                ledger, applied_at_utc
            ):
                raise ValueError("the configuration transition ledger hash changed")
        if ledger["halted"] or ledger["inflight"] != 0:
            raise ValueError("a configuration transition requires a settled ledger")
        reserved = _money(ledger["reserved_usd"], "reserved")
        ambiguous = _money(ledger["ambiguous_reserved_usd"], "ambiguous")
        if reserved != 0 or ambiguous != 0:
            try:
                liabilities = self.validate_no_replay_liabilities(
                    ledger=ledger, receipts_dir=self.receipts_dir
                )
                reserved_expected = sum(
                    (
                        _money(
                            request.get("reserved_usd"),
                            "retained reservation",
                            positive=True,
                        )
                        for request in ledger["requests"].values()
                        if request.get("state") == "orphaned_no_replay"
                    ),
                    Decimal("0"),
                )
                ambiguous_expected = sum(
                    (
                        _money(
                            request.get("reserved_usd"),
                            "ambiguous reservation",
                            positive=True,
                        )
                        for request in ledger["requests"].values()
                        if request.get("state") == "ambiguous_charge"
                    ),
                    Decimal("0"),
                )
                uncovered = [
                    key
                    for key, request in ledger["requests"].items()
                    if request.get("state")
                    in {"orphaned_no_replay", "ambiguous_charge"}
                    and key not in liabilities
                ]
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "a configuration transition requires a settled ledger or "
                    "validated no-replay holds"
                ) from error
            if (
                reserved != reserved_expected
                or ambiguous != ambiguous_expected
                or uncovered
            ):
                raise ValueError(
                    "a configuration transition requires a settled ledger or "
                    "validated no-replay holds"
                )
        if authorization["expected_identity_sha256"] != sha256_file(
            self._identity_file
        ):
            raise ValueError("the configuration transition identity hash changed")
        self._validate_transition_durable_bindings(authorization)
        if not str(authorization["reason"]).strip():
            raise ValueError("the configuration transition reason is absent")
        try:
            authorized = datetime.fromisoformat(
                str(authorization["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("the configuration transition time is invalid") from error
        if authorized.tzinfo is None:
            raise ValueError("the configuration transition time is invalid")

    def _authorize_active_config(
        self, identity: dict[str, Any], ledger: dict[str, Any]
    ) -> None:
        initial_pair = (
            identity["price_config_sha256"],
            identity["policy_sha256"],
        )
        active_pair = (
            self.active_price_config_sha256,
            sha256_file(self.policy_file),
        )
        if active_pair == initial_pair:
            if self.config_transition_file is not None:
                raise ValueError("a configuration transition is not necessary")
            return
        requested_authorization = (
            _read(self.config_transition_file)
            if self.config_transition_file is not None
            else None
        )
        requested_authorization_hash = (
            sha256_bytes(canonical_json(requested_authorization).encode())
            if requested_authorization is not None
            else None
        )
        matching_events: list[tuple[Path, dict[str, Any]]] = []
        for path in self._receipt_paths(prefix="config-transition-", suffix=".json"):
            event = self._read_transition_event(path)
            authorization = event["authorization"]
            if authorization.get("ledger_file") != str(self.ledger_file):
                raise ValueError("the applied price configuration transition changed")
            if self._transition_pairs(authorization, identity)[1] == active_pair:
                matching_events.append((path, event))
        selected_events = [
            (path, event)
            for path, event in matching_events
            if event["transition_authorization_sha256"] == requested_authorization_hash
        ]
        if requested_authorization is None:
            selected_events = matching_events
        if len(selected_events) > 1:
            raise ValueError("multiple applied price configuration transitions exist")
        if selected_events:
            event_path, event = selected_events[0]
            authorization = event["authorization"]
            authorization_hash = event["transition_authorization_sha256"]
            allowed_event_hashes = {sha256_file(event_path)}
            if self._is_ceiling_extension(authorization):
                allowed_event_hashes.add(authorization["from_config_transition_sha256"])
                matching_hashes = {sha256_file(path) for path, _ in matching_events}
                if matching_hashes != allowed_event_hashes:
                    raise ValueError(
                        "multiple applied price configuration transitions exist"
                    )
            elif len(matching_events) > 1:
                raise ValueError(
                    "multiple applied price configuration transitions exist"
                )
            active_requests = [
                request
                for request in ledger["requests"].values()
                if (
                    request.get("price_config_sha256", identity["price_config_sha256"]),
                    request.get("policy_sha256", identity["policy_sha256"]),
                )
                == active_pair
            ]
            if active_requests and any(
                request.get("config_transition_sha256") not in allowed_event_hashes
                for request in active_requests
            ):
                raise ValueError(
                    "a paid-call request lacks its authorized config transition"
                )
            if active_requests:
                self._validate_transition_durable_bindings(authorization)
            else:
                self._validate_transition_authorization(
                    authorization,
                    identity=identity,
                    ledger=ledger,
                    applied_at_utc=str(event["applied_at_utc"]),
                )
            event_hash = sha256_file(event_path)
            self._config_transition_sha256 = event_hash
            self._config_transition_event_path = event_path
            self._apply_transition_controls(authorization)
            return
        if matching_events and not self._is_ceiling_extension(
            requested_authorization or {}
        ):
            if self.config_transition_file is None:
                raise ValueError(
                    "multiple applied price configuration transitions exist"
                )
            raise ValueError("the price configuration transition file changed")
        if self.config_transition_file is None:
            raise ValueError("the active configuration requires a reviewed transition")
        authorization = requested_authorization
        assert authorization is not None
        self._validate_transition_authorization(
            authorization, identity=identity, ledger=ledger
        )
        authorization_hash = sha256_bytes(canonical_json(authorization).encode())
        event_path = self.receipts_dir / f"config-transition-{authorization_hash}.json"
        atomic_json(
            event_path,
            {
                "schema": "shared-paid-call-config-transition-event-v1",
                "authorization": authorization,
                "transition_authorization_sha256": authorization_hash,
                "applied_at_utc": _now(),
            },
            immutable=True,
        )
        self._config_transition_sha256 = sha256_file(event_path)
        self._config_transition_event_path = event_path
        self._apply_transition_controls(authorization)

    def _initialize(self) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        with self._ledger_lock():
            if self.ledger_file.is_file():
                if self._integrity_file.exists():
                    raise ValueError(
                        "the shared paid-call ledger has an integrity halt"
                    )
                if not self._identity_file.is_file():
                    raise ValueError(
                        "the shared paid-call ledger identity record is absent"
                    )
                identity = _read(self._identity_file)
                if not ledger_store.journal_base_file(self.ledger_file).is_file():
                    raise ValueError(
                        "the shared paid-call ledger has no store beside it; run "
                        "arctic-qa migrate-ledger-store --apply with every writer "
                        "stopped (docs/SHARED_MODEL_BROKER.md, Parallel bookkeeping)"
                    )
                self._validate_initial_identity(
                    identity, ledger_store.read_ledger(self.ledger_file)
                )
                ledger = self._validated_ledger()
                self._authorize_active_config(identity, ledger)
                self._publish_status(ledger)
                # A start compacts a snapshot that lags its journal, so a
                # reviewed authorization always binds the state and never a
                # stale file, and an outside reader of the plain file starts
                # from the truth.
                self._compacted_seq = ledger_store.snapshot_applied_seq(
                    self.ledger_file
                )
                self.compact(wait=True)
                return
            if self._identity_file.exists():
                raise ValueError(
                    "the shared paid-call ledger is absent after initialization"
                )
            if self.policy["live_test_maximum_papers"] != 20:
                raise ValueError(
                    "an expanded policy requires an existing reviewed ledger"
                )
            if self.prior > _money(
                self.policy["construction_review_checkpoint_usd"], "checkpoint"
            ):
                raise ValueError(
                    "prior construction spend exceeds the review checkpoint"
                )
            ledger = {
                "schema": "shared-paid-call-ledger-v1",
                "policy_sha256": sha256_file(self.policy_file),
                "price_config_sha256": self.active_price_config_sha256,
                "prior_construction_spend_usd": str(self.prior),
                "reserved_usd": "0",
                "spent_usd": "0",
                "ambiguous_reserved_usd": "0",
                "generation_submissions": 0,
                "count_requests": 0,
                "inflight": 0,
                "accepted_question_count": 0,
                "accepted_families": {},
                "family_bindings": {},
                "paper_bindings": {},
                "live_test_papers": {},
                "recent_submission_times_utc": [],
                "requests": {},
                "stages": {},
                "papers": {},
                "halted": False,
                "halt_reason": None,
                "created_at_utc": _now(),
                "updated_at_utc": _now(),
            }
            atomic_json(self._identity_file, self._ledger_identity(), immutable=True)
            self._prove_ledger(ledger)
            atomic_json(self.ledger_file, ledger)
            ledger_store.initialize_store(self.ledger_file)
            state = self._store.load()
            self._validate_ledger(state)
            self._publish_status(state)

    @staticmethod
    def _empty_usage_row() -> dict[str, Any]:
        return {
            "submissions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "reserved_usd": "0",
            "spent_usd": "0",
            "ambiguous_usd": "0",
        }

    @staticmethod
    def _empty_live_row() -> dict[str, Any]:
        return {
            "submissions": 0,
            "reserved_usd": "0",
            "spent_usd": "0",
            "ambiguous_usd": "0",
        }

    def _record_integrity_halt(self, error: Exception) -> None:
        if self._integrity_file.exists():
            return
        atomic_json(
            self._integrity_file,
            {
                "schema": "shared-paid-call-ledger-integrity-halt-v1",
                "ledger_file": str(self.ledger_file),
                "ledger_sha256": sha256_file(self.ledger_file)
                if self.ledger_file.is_file()
                else None,
                "reason": f"{type(error).__name__}: {error}",
                "recorded_at_utc": _now(),
            },
            immutable=True,
        )

    def _publish_integrity_halt(self, error: Exception) -> None:
        try:
            previous = _read(self._status_file) if self._status_file.is_file() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
        status = {
            **previous,
            "schema": "shared-gemini-broker-status-v2",
            "ledger_file": str(self.ledger_file),
            "ledger_sha256": sha256_file(self.ledger_file)
            if self.ledger_file.is_file()
            else None,
            "policy_sha256": sha256_file(self.policy_file),
            "price_config_sha256": sha256_file(self.price_config_file),
            "updated_at_utc": _now(),
            "stages": previous.get("stages", {}),
            "papers": previous.get("papers", {}),
            "limits": previous.get("limits", {}),
            "usage": previous.get("usage", {}),
            "remaining": previous.get("remaining", {}),
            "halted": True,
            "halt_reason": f"{type(error).__name__}: {error}",
            "integrity_valid": False,
            "status_state": "integrity_halted",
        }
        atomic_json(self._status_file, status)
        if self._status_observer is not None:
            self._status_observer(self._status_file)

    def _receipt_names(self) -> list[str]:
        """List the receipts directory once, and keep the listing.

        The immutable-event proof used to walk the directory nine times, once
        per pattern. At 23,615 entries on a rotating disk each walk cost 22 to
        40 ms, which was 320 ms of every ledger read that had anything to
        prove. A receipt is immutable, so the listing is a pure function of
        the directory, and a directory whose modification time and size have
        not moved has the same entries.

        Under paper concurrency the directory moves on every paid call of
        every worker, so the fingerprint alone made the listing a full
        ``scandir`` of 32,251 entries on nearly every ledger read: 56 ms of
        the 371 ms that one warm proof cost on 2026-09-17. A concurrent
        broker therefore re-lists at most every
        ``RECEIPT_LISTING_REFRESH_SECONDS``. The staleness is bounded and is
        far tighter than the full immutable-event pass, which runs every
        ``IMMUTABLE_EVENT_REVALIDATION_SECONDS`` and re-lists first. A
        sequential broker, which is every reviewed operation, keeps the exact
        fingerprint and sees a receipt the moment it lands.
        """
        listing = getattr(self, "_receipt_listing", None)
        refresh = (
            RECEIPT_LISTING_REFRESH_SECONDS
            if getattr(self, "concurrent_construction", False)
            else 0.0
        )
        if listing is not None and refresh and time.monotonic() - listing[2] < refresh:
            return listing[1]
        try:
            status = os.stat(self.receipts_dir)
            fingerprint = (status.st_mtime_ns, status.st_size)
        except OSError:
            fingerprint = (0, 0)
        # A probe built without ``__init__`` (``validate_no_replay_liabilities``)
        # has no listing yet. The live v12 transition was refused at 04:58 UTC
        # on 2026-09-17 because this read an attribute the probe lacked, and
        # the refusal was read as an unsettled ledger.
        if listing is not None and listing[0] == fingerprint:
            return listing[1]
        names = sorted(
            entry.name
            for entry in os.scandir(self.receipts_dir)
            if entry.is_file(follow_symlinks=False)
        )
        self._receipt_listing = (fingerprint, names, time.monotonic())
        # Everything derived from the listing alone is derived again.
        self._receipt_derived = {}
        return names

    def _listing_derived(self, key: Any, build: Any) -> Any:
        """Return a value derived from the receipts listing alone, once.

        The listing is a pure function of the directory and a receipt is
        immutable, so anything read out of the names, or out of the files the
        names point at, is a pure function of the same listing. Deriving it
        again on every ledger read cost 64 ms of a warm proof for the six
        name filters and the name-to-request-key scan alone.
        """
        self._receipt_names()
        derived = getattr(self, "_receipt_derived", None)
        if derived is None:
            derived = {}
            self._receipt_derived = derived
        if key not in derived:
            derived[key] = build()
        return derived[key]

    def _receipt_paths(self, *, prefix: str = "", suffix: str = "") -> list[Path]:
        """The receipts whose name has this prefix and this suffix.

        The list is cached against the listing it came from, so a caller reads
        it and never changes it.
        """
        return self._listing_derived(
            ("paths", prefix, suffix),
            lambda: [
                self.receipts_dir / name
                for name in self._receipt_names()
                if name.startswith(prefix) and name.endswith(suffix)
            ],
        )

    def _receipt_request_keys(self) -> set[str]:
        """The request key of every immutable paid-call receipt on disk.

        The proof that no such receipt is absent from the ledger used to match
        the pattern against all 32,251 names on every read, which was 33 ms.
        The names are immutable, so the keys they carry are derived once per
        listing and the proof itself is a subset test.
        """
        return self._listing_derived(
            "request_keys",
            lambda: {
                match.group(1)
                for match in (
                    PAID_CALL_RECEIPT_NAME.fullmatch(name)
                    for name in self._receipt_names()
                )
                if match
            },
        )

    def _ledger_evidence_fingerprint(self) -> tuple[Any, ...]:
        """Identify the bytes the immutable-event proof is a proof of.

        The proof is a pure function of the ledger file and of the receipt
        files beside it, so the same fingerprint carries the same answer. The
        ledger is hashed, which is 7 milliseconds at 7 MB. The receipts
        directory is stated, which names every addition and every removal; a
        change inside an existing receipt does not move it, and that is what
        the full pass every ``IMMUTABLE_EVENT_REVALIDATION_SECONDS`` is for.
        """
        try:
            stat = os.stat(self.receipts_dir)
            receipts = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            receipts = (0, 0)
        return (self._store.sequence, receipts)

    def _validated_ledger(self) -> dict[str, Any]:
        if self._integrity_file.exists():
            raise ValueError("the shared paid-call ledger has an integrity halt")
        try:
            if self._store.is_dirty():
                # A mutation was abandoned without a commit: a refused
                # reservation, an exception. The touched keys go back to the
                # committed values in place, which costs the touched keys and
                # not a reload of the whole snapshot.
                self._store.rollback()
            ledger = self._store.read()
            full, changed = self._store.take_changes()
            if full:
                self._validate_ledger(ledger)
                # The requests map was replaced whole. Which rows moved is not
                # known, so the immutable-event proof checks every signature
                # again rather than trust a list it was never given.
                self._immutable_events_pending = None
            elif changed is not None:
                self._validate_ledger_delta(ledger, changed)
                if self._immutable_events_pending is not None:
                    self._immutable_events_pending.update(changed)
            fingerprint = self._ledger_evidence_fingerprint()
            # The ledger's own consistency is proved on every read, above. The
            # proof against the receipts on disk is skipped only when nothing
            # it reads has changed since the last time it ran.
            stale = (
                self._immutable_events_proved_at is None
                or time.monotonic() - self._immutable_events_proved_at
                >= IMMUTABLE_EVENT_REVALIDATION_SECONDS
            )
            if stale or fingerprint != self._ledger_evidence_proved:
                self._validate_immutable_events(ledger)
                self._validate_active_transition_event(ledger)
                self._ledger_evidence_proved = fingerprint
            return ledger
        except Exception as error:
            self._record_integrity_halt(error)
            self._publish_integrity_halt(error)
            raise ValueError(
                f"the shared paid-call ledger failed integrity validation: {error}"
            ) from error

    def _validate_request_events(
        self,
        ledger: dict[str, Any],
        *,
        request_key: str,
        request: dict[str, Any],
        base_fields: tuple[str, ...],
        allowed_pairs: set[tuple[str, str]],
        initial_pair: tuple[str, str],
        transition_events_by_pair: dict[tuple[str, str], set[str]],
        reconciliation_events: dict[str, tuple[Path, dict[str, Any]]],
    ) -> None:
        """Prove one ledger row against its immutable events on disk.

        This is the body of the per-request pass of
        :meth:`_validate_immutable_events`, unchanged. It is a method of its
        own so the pass can skip a row it has already proved.
        """
        self._validate_count_retry_binding(request_key, request, base_fields)
        resume_receipt_sha256 = request.get("resumed_from_not_submitted_sha256")
        resumed = resume_receipt_sha256 is not None
        if resumed:
            original_path = self.receipts_dir / f"{request_key}.json"
            if (
                not re.fullmatch(r"[a-f0-9]{64}", str(resume_receipt_sha256 or ""))
                or not original_path.is_file()
                or sha256_file(original_path) != resume_receipt_sha256
            ):
                raise ValueError("a resumed request lost its not-submitted receipt")
            original = _read(original_path)
            stable_fields = tuple(
                name
                for name in base_fields
                if name
                not in {
                    "gate_sha256",
                    "config_transition_sha256",
                    "price_config_sha256",
                    "policy_sha256",
                }
            )
            if (
                any(original.get(name) != request.get(name) for name in stable_fields)
                or original.get("state") != "not_submitted"
                or original.get("reason") not in RESUMABLE_NOT_SUBMITTED_REASONS
                or original.get("live_call_made") is not False
                or original.get("config_transition_sha256")
                != request.get("resumed_from_config_transition_sha256")
            ):
                raise ValueError("a resumed request changed its prior identity")
        request_config_hash = request.get(
            "price_config_sha256", ledger["price_config_sha256"]
        )
        request_policy_hash = request.get("policy_sha256", ledger["policy_sha256"])
        request_pair = (request_config_hash, request_policy_hash)
        if request_pair not in allowed_pairs:
            raise ValueError("a paid-call request uses an unauthorized configuration")
        transition_hash = request.get("config_transition_sha256")
        if request_pair == initial_pair:
            if transition_hash is not None:
                raise ValueError("an initial-config request has a transition binding")
        elif transition_hash not in transition_events_by_pair.get(request_pair, set()):
            raise ValueError(
                "a paid-call request lacks its authorized config transition"
            )
        event_stem = self._request_event_stem(request_key, request)
        submitted_path = self.receipts_dir / f"{event_stem}.submitted.json"
        final_path = self.receipts_dir / f"{event_stem}.json"
        if submitted_path.is_file():
            submitted = _read(submitted_path)
            if any(submitted.get(name) != request.get(name) for name in base_fields):
                raise ValueError("an immutable submitted event changed identity")
            if request.get("state") in {
                "submitted",
                "orphaned_no_replay",
                "completed",
                "ambiguous_charge",
            } and _money(
                submitted.get("reserved_usd"),
                "submitted reservation",
                positive=True,
            ) != _money(
                request.get("reserved_usd"), "ledger reservation", positive=True
            ):
                raise ValueError("an immutable submitted reservation changed")
        state = request.get("state")
        if state not in {
            "completed",
            "ambiguous_charge",
            "count_error",
            "too_large_not_ready",
            "not_submitted",
        }:
            # The row is not terminal, so it has no final event to prove yet.
            return
        if not final_path.is_file():
            raise ValueError("a terminal paid request lacks its immutable receipt")
        final = _read(final_path)
        if any(final.get(name) != request.get(name) for name in base_fields):
            raise ValueError("an immutable final event changed request identity")
        count_error_path = (
            self.receipts_dir / f"{request_key}.count-error-continuation.json"
        )
        count_error_sha256 = request.get("count_error_continuation_sha256")
        if count_error_sha256 is not None:
            event = _read(count_error_path) if count_error_path.is_file() else {}
            review_path = Path(str(event.get("review_file") or ""))
            evidence_path = Path(str(event.get("evidence_file") or ""))
            # The event binds the count-error receipt of the request key.
            # A reviewed continuation that authorized a retry keeps that
            # binding while the request counts again under a later round.
            reviewed_path = self.receipts_dir / f"{request_key}.json"
            reviewed = _read(reviewed_path) if reviewed_path.is_file() else {}
            if (
                (state != "count_error" and not request.get("count_retry_round"))
                or not re.fullmatch(r"[a-f0-9]{64}", str(count_error_sha256))
                or not count_error_path.is_file()
                or sha256_file(count_error_path) != count_error_sha256
                or event.get("schema") != COUNT_ERROR_CONTINUATION_SCHEMA
                or event.get("request_key") != request_key
                or not reviewed_path.is_file()
                or event.get("count_error_receipt_sha256") != sha256_file(reviewed_path)
                or event.get("gate_sha256") != reviewed.get("gate_sha256")
                or event.get("live_call_made") is not False
                or event.get("replay_prohibited") is not True
                or not review_path.is_file()
                or event.get("review_file_sha256") != sha256_file(review_path)
                or not evidence_path.is_file()
                or event.get("evidence_file_sha256") != sha256_file(evidence_path)
            ):
                raise ValueError("a count-error continuation event changed")
        elif count_error_path.is_file():
            raise ValueError("an unapplied count-error continuation event exists")
        rejection_path = (
            self.receipts_dir / f"{request_key}.http-rejection-settlement.json"
        )
        rejection_sha256 = request.get("http_rejection_settlement_sha256")
        if rejection_sha256 is not None:
            if (
                state != "completed"
                or not re.fullmatch(r"[a-f0-9]{64}", str(rejection_sha256))
                or not rejection_path.is_file()
                or sha256_file(rejection_path) != rejection_sha256
                or not self._http_rejection_settlement_valid(
                    rejection_path, request, final
                )
            ):
                raise ValueError("an http rejection settlement event changed")
        elif rejection_path.is_file():
            raise ValueError("an unapplied http rejection settlement event exists")
        settlement_path = (
            self.receipts_dir / f"{request_key}.pretransport-settlement.json"
        )
        settlement_sha256 = request.get("pretransport_settlement_sha256")
        if settlement_sha256 is not None:
            if (
                state != "completed"
                or not settlement_path.is_file()
                or sha256_file(settlement_path) != settlement_sha256
                or not self._pretransport_settlement_valid(
                    settlement_path, request, final
                )
            ):
                raise ValueError("a pretransport settlement event changed")
        elif settlement_path.is_file():
            raise ValueError("an unapplied pretransport settlement event exists")
        reconciliation = reconciliation_events.pop(request_key, None)
        reconciliation_sha256 = request.get("usage_reconciliation_sha256")
        if reconciliation_sha256 is not None:
            if state != "completed" or reconciliation is None:
                raise ValueError("a usage reconciliation event is absent")
            reconciliation_path, event = reconciliation
            received_path = self.receipts_dir / f"{event_stem}.received.json"
            if (
                not re.fullmatch(r"[a-f0-9]{64}", reconciliation_sha256)
                or sha256_file(reconciliation_path) != reconciliation_sha256
                or final.get("state") != "ambiguous_charge"
                or not received_path.is_file()
                or event["received_receipt_sha256"] != sha256_file(received_path)
                or event["ambiguous_receipt_sha256"] != sha256_file(final_path)
                or event["config_transition_sha256"]
                != request.get("config_transition_sha256")
                or event["price_config_sha256"] != request.get("price_config_sha256")
                or event["normalized_usage"] != request.get("usage")
                or _money(event["actual_cost_usd"], "reconciled actual cost")
                != _money(request.get("actual_cost_usd"), "ledger actual cost")
            ):
                raise ValueError("a usage reconciliation event changed")
        elif rejection_sha256 is not None:
            if reconciliation is not None:
                raise ValueError("an unapplied usage reconciliation event exists")
        else:
            if reconciliation is not None and state != "ambiguous_charge":
                raise ValueError("an unapplied usage reconciliation event exists")
            if final.get("state") != state:
                raise ValueError("an immutable final event changed request state")
            if state == "completed" and (
                _money(final.get("actual_cost_usd"), "final actual cost")
                != _money(request.get("actual_cost_usd"), "ledger actual cost")
                or final.get("usage") != request.get("usage")
            ):
                raise ValueError("an immutable final event changed cost or usage")

    def _validate_active_transition_event(self, ledger: dict[str, Any]) -> None:
        path = self._config_transition_event_path
        if path is None:
            return
        if not path.is_file():
            raise ValueError("the applied price configuration transition is absent")
        if sha256_file(path) != self._config_transition_sha256:
            raise ValueError("the applied price configuration transition changed")
        event = self._read_transition_event(path)
        authorization = event["authorization"]
        identity = _read(self._identity_file)
        initial_pair = (
            identity["price_config_sha256"],
            identity["policy_sha256"],
        )
        active_pair = (
            self.active_price_config_sha256,
            sha256_file(self.policy_file),
        )
        matching_paths = []
        for candidate_path in self._receipt_paths(
            prefix="config-transition-", suffix=".json"
        ):
            candidate = self._read_transition_event(candidate_path)
            candidate_authorization = candidate["authorization"]
            if (
                candidate_authorization["ledger_file"] == str(self.ledger_file)
                and self._transition_pairs(candidate_authorization, identity)[1]
                == active_pair
            ):
                matching_paths.append(candidate_path)
        allowed_paths = {path}
        if self._is_ceiling_extension(authorization):
            predecessor = authorization["from_config_transition_sha256"]
            predecessor_paths = [
                candidate_path
                for candidate_path in matching_paths
                if sha256_file(candidate_path) == predecessor
            ]
            if len(predecessor_paths) != 1:
                raise ValueError("the policy transition predecessor changed")
            allowed_paths.add(predecessor_paths[0])
        if set(matching_paths) != allowed_paths:
            if len(matching_paths) > 1:
                raise ValueError(
                    "multiple applied price configuration transitions exist"
                )
            raise ValueError("the applied price configuration transition changed")
        active_requests = [
            request
            for request in ledger["requests"].values()
            if (
                request.get("price_config_sha256", initial_pair[0]),
                request.get("policy_sha256", initial_pair[1]),
            )
            == active_pair
        ]
        if active_requests:
            self._validate_transition_durable_bindings(authorization)
        else:
            self._validate_transition_authorization(
                authorization,
                identity=identity,
                ledger=ledger,
                applied_at_utc=str(event["applied_at_utc"]),
            )
        if (
            authorization["ledger_file"] != str(self.ledger_file)
            or self._transition_pairs(authorization, identity)[1] != active_pair
            or authorization["expected_identity_sha256"]
            != sha256_file(self._identity_file)
        ):
            raise ValueError("the applied price configuration transition changed")

    def _accepted_item_events(self) -> dict[str, str]:
        """The accepted item of each family, from the immutable events alone.

        Every accepted-item receipt is read and every supersession chain is
        walked, which is 96 file reads and their hashes on the live ledger.
        The receipts are immutable, so the answer is a pure function of the
        listing and is derived once per listing, not once per ledger read.
        """

        def build() -> dict[str, str]:
            accepted_roots: dict[str, tuple[str, Path]] = {}
            accepted_successors: dict[
                tuple[str, str], tuple[str, Path, dict[str, Any]]
            ] = {}
            for path in self._receipt_paths(prefix="accepted-", suffix=".json"):
                value = _read(path)
                family_id = value.get("family_id")
                item_id = value.get("item_id")
                if value.get("schema") == ACCEPTED_ITEM_V1_SCHEMA:
                    expected_name = (
                        f"accepted-{sha256_bytes(str(family_id).encode())}.json"
                    )
                    if (
                        set(value)
                        != {"schema", "family_id", "item_id", "recorded_at_utc"}
                        or not isinstance(family_id, str)
                        or not family_id
                        or not isinstance(item_id, str)
                        or not item_id
                        or path.name != expected_name
                        or family_id in accepted_roots
                    ):
                        raise ValueError(
                            "an accepted-item event has an invalid identity"
                        )
                    accepted_roots[family_id] = (item_id, path)
                    continue
                if value.get("schema") != ACCEPTED_ITEM_V2_SCHEMA:
                    raise ValueError("an accepted-item event has an invalid schema")
                predecessor_item_id = value.get("predecessor_item_id")
                expected_name = (
                    f"accepted-{sha256_bytes(str(family_id).encode())}-"
                    f"{sha256_bytes(str(item_id).encode())}.json"
                )
                successor_key = (str(family_id), str(predecessor_item_id))
                if (
                    set(value)
                    != {
                        "schema",
                        "family_id",
                        "item_id",
                        "predecessor_item_id",
                        "predecessor_event_sha256",
                        "invocation_run_id",
                        "gate_sha256",
                        "recorded_at_utc",
                    }
                    or not isinstance(family_id, str)
                    or not family_id
                    or not isinstance(item_id, str)
                    or not item_id
                    or not isinstance(predecessor_item_id, str)
                    or not predecessor_item_id
                    or predecessor_item_id == item_id
                    or not re.fullmatch(
                        r"[a-f0-9]{64}",
                        str(value.get("predecessor_event_sha256") or ""),
                    )
                    or not re.fullmatch(
                        r"[a-f0-9]{64}", str(value.get("gate_sha256") or "")
                    )
                    or not str(value.get("invocation_run_id") or "").strip()
                    or path.name != expected_name
                    or successor_key in accepted_successors
                ):
                    raise ValueError("an accepted-item event has an invalid identity")
                accepted_successors[successor_key] = (item_id, path, value)

            accepted_events: dict[str, str] = {}
            visited_successors: set[tuple[str, str]] = set()
            for family_id, (root_item_id, root_path) in accepted_roots.items():
                item_id = root_item_id
                event_path = root_path
                chain_items = {item_id}
                while (family_id, item_id) in accepted_successors:
                    key = (family_id, item_id)
                    next_item_id, next_path, event = accepted_successors[key]
                    if (
                        event["predecessor_event_sha256"] != sha256_file(event_path)
                        or next_item_id in chain_items
                    ):
                        raise ValueError("an accepted-item supersession chain changed")
                    visited_successors.add(key)
                    chain_items.add(next_item_id)
                    item_id = next_item_id
                    event_path = next_path
                accepted_events[family_id] = item_id
            if len(visited_successors) != len(accepted_successors):
                raise ValueError("an accepted-item supersession lacks its predecessor")
            return accepted_events

        return self._listing_derived("accepted_events", build)

    def _validate_immutable_events(self, ledger: dict[str, Any]) -> None:
        identity = _read(self._identity_file)
        initial_pair = (
            ledger["price_config_sha256"],
            ledger["policy_sha256"],
        )
        allowed_pairs = {initial_pair}
        transition_events_by_pair: dict[tuple[str, str], set[str]] = {}
        transition_authorizations_by_hash: dict[str, dict[str, Any]] = {}
        pending_events: list[
            tuple[Path, dict[str, Any], tuple[str, str], tuple[str, str]]
        ] = []
        for path in self._receipt_paths(prefix="config-transition-", suffix=".json"):
            event = self._read_transition_event(path)
            authorization = event["authorization"]
            from_pair, target_pair = self._transition_pairs(authorization, identity)
            if authorization["ledger_file"] != str(self.ledger_file) or authorization[
                "expected_identity_sha256"
            ] != sha256_file(self._identity_file):
                raise ValueError("the applied price configuration transition changed")
            pending_events.append((path, authorization, from_pair, target_pair))
        while pending_events:
            progressed = False
            for item in list(pending_events):
                path, authorization, from_pair, target_pair = item
                if from_pair not in allowed_pairs:
                    continue
                event_hash = sha256_file(path)
                existing_hashes = transition_events_by_pair.get(target_pair, set())
                if existing_hashes:
                    predecessor = authorization.get("from_config_transition_sha256")
                    prior = transition_authorizations_by_hash.get(str(predecessor))
                    if (
                        not self._is_ceiling_extension(authorization)
                        or existing_hashes != {predecessor}
                        or prior is None
                        or prior.get("changed_policy_fields") != UNBOUNDED_COUNT_CHANGE
                        or _money(
                            prior.get("maximum_authorized_cumulative_tranche_usd"),
                            "predecessor tranche",
                            positive=True,
                        )
                        != Decimal("5")
                        or _money(
                            authorization.get(
                                "maximum_authorized_cumulative_tranche_usd"
                            ),
                            "transition tranche",
                            positive=True,
                        )
                        != Decimal("10")
                    ):
                        raise ValueError(
                            "multiple applied price configuration transitions exist"
                        )
                if authorization["schema"] == "shared-paid-call-config-transition-v1":
                    if from_pair != initial_pair:
                        raise ValueError(
                            "the applied price configuration transition changed"
                        )
                else:
                    if not self._is_ceiling_extension(authorization):
                        prior_hashes = transition_events_by_pair.get(from_pair, set())
                        predecessor = authorization["from_config_transition_sha256"]
                        if from_pair == initial_pair:
                            valid_predecessor = predecessor is None
                        else:
                            if predecessor not in prior_hashes:
                                continue
                            valid_predecessor = predecessor in prior_hashes
                            if (
                                valid_predecessor
                                and authorization.get("changed_policy_fields")
                                == LIVE_TEST_BUDGET_EXTENSION_CHANGE
                            ):
                                prior = transition_authorizations_by_hash.get(
                                    predecessor
                                )
                                valid_predecessor = bool(
                                    prior
                                    and self._is_ceiling_extension(prior)
                                    and _money(
                                        prior.get(
                                            "maximum_authorized_cumulative_tranche_usd"
                                        ),
                                        "predecessor tranche",
                                        positive=True,
                                    )
                                    == Decimal("10")
                                )
                        if not valid_predecessor:
                            raise ValueError(
                                "the policy transition predecessor changed"
                            )
                allowed_pairs.add(target_pair)
                transition_events_by_pair.setdefault(target_pair, set()).add(event_hash)
                transition_authorizations_by_hash[event_hash] = authorization
                pending_events.remove(item)
                progressed = True
            if not progressed:
                raise ValueError("the applied configuration transition chain changed")
        reconciliation_events: dict[str, tuple[Path, dict[str, Any]]] = {}
        for path in self._receipt_paths(suffix=".usage-reconciliation.json"):
            event = self._read_usage_reconciliation(path)
            request_key = event["request_key"]
            if request_key in reconciliation_events:
                raise ValueError("multiple usage reconciliation events exist")
            reconciliation_events[request_key] = (path, event)
        if not self._receipt_request_keys() <= ledger["requests"].keys():
            raise ValueError("an immutable paid-call event is absent from the ledger")
        base_fields = (
            "request_key",
            "request_sha256",
            "run_id",
            "stage",
            "paper_id",
            "family_id",
            "source_version_id",
            "model",
            "gate_sha256",
            "price_config_sha256",
            "policy_sha256",
            "config_transition_sha256",
        )
        # A request whose row and whose context are unchanged was proved by
        # an earlier pass of this same process, and its evidence on disk is
        # immutable. Re-proving all 5,000 of them on every ledger read cost
        # about 2.4 seconds, and a paid call reads the ledger about seven
        # times: 17 seconds of the 25-second admission that serialised the
        # four paper threads on 2026-09-17. The proof is kept per row and
        # replayed only for a row that moved, and a full pass runs again every
        # ``IMMUTABLE_EVENT_REVALIDATION_SECONDS`` whatever the rows say.
        context = (
            sha256_file(self._identity_file),
            initial_pair,
            tuple(sorted(str(value) for value in transition_authorizations_by_hash)),
        )
        full_pass = (
            self._immutable_events_proved_at is None
            or time.monotonic() - self._immutable_events_proved_at
            >= IMMUTABLE_EVENT_REVALIDATION_SECONDS
            or self._immutable_events_context != context
        )
        if full_pass:
            self._immutable_events_proved = {}
            self._immutable_events_context = context
            self._ledger_evidence_proved = None
            # The custody of a terminal receipt is proved with the rows, so a
            # receipt taken away behind this process's back is caught by the
            # same full pass, within IMMUTABLE_EVENT_REVALIDATION_SECONDS.
            self._custody_proved = set()
            self._receipt_listing = None
        proved = self._immutable_events_proved
        requests = ledger["requests"]
        # Which rows owe the proof. A signature of each of the 8,230 rows cost
        # 136 ms of the 371 ms one warm proof took on 2026-09-17, and a paid
        # call reads the ledger five to seven times. The store already reports
        # the rows a record moved, which is the same tracking the money proof
        # of the delta trusts, so a signature is taken only where that report
        # is absent: a reload of the snapshot, a reviewed repair, a full pass.
        pending = None if full_pass else self._immutable_events_pending
        signatures: dict[str, str] = {}
        targets: list[str] = []
        if pending is None:
            for request_key, request in requests.items():
                signature = canonical_json(request)
                if proved.get(request_key) != signature:
                    signatures[request_key] = signature
                    targets.append(request_key)
        else:
            targets = [
                request_key for request_key in requests if request_key not in proved
            ]
            targets.extend(
                request_key
                for request_key in pending
                if request_key in requests and request_key in proved
            )
        for request_key in targets:
            request = requests[request_key]
            self._validate_request_events(
                ledger,
                request_key=request_key,
                request=request,
                base_fields=base_fields,
                allowed_pairs=allowed_pairs,
                initial_pair=initial_pair,
                transition_events_by_pair=transition_events_by_pair,
                reconciliation_events=reconciliation_events,
            )
            signature = signatures.get(request_key)
            proved[request_key] = (
                canonical_json(request) if signature is None else signature
            )
        self._immutable_events_pending = set()
        if full_pass:
            self._immutable_events_proved_at = time.monotonic()

        for request_key in list(reconciliation_events):
            if request_key in proved:
                # The row this event belongs to was proved, by this pass or by
                # an earlier one, and that proof accounted for the event.
                reconciliation_events.pop(request_key)
        if reconciliation_events:
            raise ValueError("a usage reconciliation event lacks a ledger request")

        self._ambiguous_continuation_events(ledger)
        self._orphaned_continuation_events(ledger)

        if self._accepted_item_events() != ledger["accepted_families"]:
            raise ValueError("the accepted-item ledger differs from immutable events")

    def _http_rejection_settlement_valid(
        self, path: Path, request: dict[str, Any], final: dict[str, Any]
    ) -> bool:
        """Check one settled provider rejection against its immutable events."""
        try:
            event = _read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        identity_keys = (
            "run_id",
            "stage",
            "paper_id",
            "family_id",
            "source_version_id",
            "request_sha256",
            "reserved_usd",
            "submitted_at_utc",
        )
        if (
            not isinstance(event, dict)
            or set(event) != HTTP_REJECTION_SETTLEMENT_FIELDS
            or event.get("schema") != HTTP_REJECTION_SETTLEMENT_SCHEMA
            or event.get("request_key") != request.get("request_key")
            or event.get("request_identity")
            != {key: request.get(key) for key in identity_keys}
            or event.get("ambiguous_receipt_sha256")
            != sha256_file(self.receipts_dir / f"{request['request_key']}.json")
            or final.get("state") != "ambiguous_charge"
            or final.get("error_class") != "known_http_response_unknown_charge"
            or final.get("http_status") not in HTTP_REJECTION_STATUSES
            or event.get("http_status") != final.get("http_status")
            or event.get("provider_error_status")
            not in HTTP_REJECTION_PROVIDER_STATUSES
            or event.get("gate_sha256") != request.get("gate_sha256")
            or event.get("live_call_made") is not True
            or event.get("generation_started") is not False
            or event.get("replay_prohibited") is not True
            or _money(event.get("actual_cost_usd"), "settled cost") != 0
            or _money(request.get("actual_cost_usd"), "ledger cost") != 0
            or request.get("usage") != _ZERO_USAGE
        ):
            return False
        for name in ("review_file", "evidence_file"):
            file_path = Path(str(event.get(name) or ""))
            if not file_path.is_file() or event.get(f"{name}_sha256") != sha256_file(
                file_path
            ):
                return False
        if event.get("error_body_source") == "reproduction":
            record = Path(str(event.get("reproduction_record") or ""))
            if not record.is_file() or event.get(
                "reproduction_record_sha256"
            ) != sha256_file(record):
                return False
        elif event.get("error_body_source") != "receipt" or final.get(
            "provider_error_status"
        ) != event.get("provider_error_status"):
            return False
        return True

    def _pretransport_settlement_valid(
        self, path: Path, request: dict[str, Any], final: dict[str, Any]
    ) -> bool:
        try:
            event = _read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        required = {
            "schema",
            "request_key",
            "ledger_sha256_before",
            "request_identity",
            "sidecars_absent",
            "traceback_evidence_file",
            "traceback_evidence_sha256",
            "review_file",
            "review_file_sha256",
            "gate_sha256",
            "config_transition_sha256",
            "actual_cost_usd",
            "live_call_made",
            "settled_at_utc",
        }
        identity_keys = {
            "run_id",
            "stage",
            "paper_id",
            "family_id",
            "source_version_id",
            "request_sha256",
            "reserved_usd",
            "submitted_at_utc",
        }
        if (
            not isinstance(event, dict)
            or set(event) != required
            or event.get("schema") != PRETRANSPORT_SETTLEMENT_SCHEMA
            or event.get("request_key") != request.get("request_key")
            or event.get("request_identity")
            != {key: request.get(key) for key in identity_keys}
            or event.get("sidecars_absent")
            != ["final", "received", "submitted", "trace"]
            or event.get("actual_cost_usd") != "0"
            or event.get("live_call_made") is not False
            or event.get("config_transition_sha256")
            != request.get("config_transition_sha256")
            or event.get("gate_sha256") != request.get("gate_sha256")
            or final.get("state") != "completed"
            or final.get("live_call_made") is not False
            or final.get("actual_cost_usd") != "0"
            or final.get("usage")
            != {
                "promptTokenCount": 0,
                "candidatesTokenCount": 0,
                "thoughtsTokenCount": 0,
            }
            or final.get("pretransport_settlement_sha256") != sha256_file(path)
        ):
            return False
        for path_key, hash_key in (
            ("traceback_evidence_file", "traceback_evidence_sha256"),
            ("review_file", "review_file_sha256"),
        ):
            evidence_path = Path(str(event.get(path_key) or ""))
            if not evidence_path.is_file() or sha256_file(evidence_path) != event.get(
                hash_key
            ):
                return False
        return True

    def _read_usage_reconciliation(self, path: Path) -> dict[str, Any]:
        event = _read(path)
        if (
            not isinstance(event, dict)
            or set(event) - USAGE_RECONCILIATION_OPTIONAL_FIELDS
            != USAGE_RECONCILIATION_FIELDS
        ):
            raise ValueError("a usage reconciliation event changed")
        request_key = event.get("request_key")
        if (
            event.get("schema") != "shared-paid-call-usage-reconciliation-v1"
            or not isinstance(request_key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_key)
            or path.name != f"{request_key}.usage-reconciliation.json"
        ):
            raise ValueError("a usage reconciliation event changed")
        for field in (
            "received_receipt_sha256",
            "ambiguous_receipt_sha256",
            "price_config_sha256",
            "ledger_sha256_before",
            "gate_sha256",
            "review_record_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(event.get(field) or "")):
                raise ValueError("a usage reconciliation event changed")
        transition = event.get("config_transition_sha256")
        if transition is not None and not re.fullmatch(
            r"[a-f0-9]{64}", str(transition)
        ):
            raise ValueError("a usage reconciliation event changed")
        usage = _normalized_usage({"usageMetadata": event.get("normalized_usage")})
        # Exactly one token count is the proved zero: the thinking count of the
        # older shape, or the answer count of the shape gemini-3.7-flash
        # returned on 2026-09-16.
        omitted = event.get("omitted_zero_usage_field") or "thoughtsTokenCount"
        if (
            usage != event["normalized_usage"]
            or omitted not in ("thoughtsTokenCount", "candidatesTokenCount")
            or usage[omitted] != 0
        ):
            raise ValueError("a usage reconciliation event changed")
        # The listing, not a glob: a glob walks all 32,251 receipt names
        # again, which was 44 ms of every ledger read that proved this event.
        matching_receipts = [
            candidate
            for candidate in self._receipt_paths(prefix=request_key, suffix=".json")
            if sha256_file(candidate) == event["ambiguous_receipt_sha256"]
        ]
        if len(matching_receipts) != 1:
            raise ValueError("the usage reconciliation request model is unavailable")
        request_record = _read(matching_receipts[0])
        stage = str(request_record.get("stage") or "")
        # A construction-only broker has no evaluation price config, so it
        # cannot recompute the cost of an evaluation stage. It still validates
        # every hash, the usage record and the review of the event, and it
        # reads the recorded cost, which the ledger totals already prove.
        # An evaluation-capable broker recomputes the cost.
        recomputable = not is_evaluation_stage(stage) or (
            self.evaluation_config is not None
        )
        if recomputable:
            request_config = self.config_for_stage(stage)
            if request_record.get("model") != request_config["model"]:
                raise ValueError("the usage reconciliation request model changed")
            actual = _cost(
                request_config,
                usage["promptTokenCount"],
                usage["candidatesTokenCount"] + usage["thoughtsTokenCount"],
            )
            if _money(event.get("actual_cost_usd"), "reconciled cost") != actual:
                raise ValueError("a usage reconciliation event changed")
        else:
            _money(event.get("actual_cost_usd"), "reconciled cost")
        review_path = Path(str(event.get("review_record") or "")).resolve()
        if (
            not str(event.get("integrated_code_commit") or "").strip()
            or not review_path.is_file()
            or event["review_record_sha256"] != sha256_file(review_path)
        ):
            raise ValueError("a usage reconciliation review changed")
        try:
            reconciled = datetime.fromisoformat(
                str(event["reconciled_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("a usage reconciliation time changed") from error
        if reconciled.tzinfo is None:
            raise ValueError("a usage reconciliation time changed")
        return event

    def _read_ambiguous_continuation(self, path: Path) -> dict[str, Any]:
        event = _read(path)
        if (
            isinstance(event, dict)
            and event.get("schema") == RECEIVED_MAX_TOKENS_CONTINUATION_SCHEMA
        ):
            return self._validate_received_max_tokens_continuation(event, path)
        if (
            isinstance(event, dict)
            and event.get("schema") == PROVIDER_TIMEOUT_CONTINUATION_SCHEMA
        ):
            return self._validate_provider_timeout_continuation(event, path)
        if (
            not isinstance(event, dict)
            or set(event) != AMBIGUOUS_CONTINUATION_FIELDS
            or event.get("schema") != AMBIGUOUS_CONTINUATION_SCHEMA
        ):
            raise ValueError("an ambiguous continuation event changed")
        request_key = event.get("request_key")
        if (
            not isinstance(request_key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_key)
            or path.name != f"ambiguous-continuation-{request_key}.json"
        ):
            raise ValueError("an ambiguous continuation event changed")
        for field in (
            "ambiguous_receipt_sha256",
            "evidence_file_sha256",
            "review_file_sha256",
            "ledger_sha256_before",
            "gate_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(event.get(field) or "")):
                raise ValueError("an ambiguous continuation event changed")
        if (
            event.get("error_class") != "known_http_response_unknown_charge"
            or not _is_server_error_status(event.get("http_status"))
            or event.get("live_call_made") is not True
            or event.get("received_receipt_absent") is not True
            or event.get("reservation_policy")
            != AMBIGUOUS_CONTINUATION_RESERVATION_POLICY
            or event.get("scope") != "unrelated_families_only"
            or event.get("skip_reason_code") != "operational_ambiguous_charge_http_500"
            or not str(event.get("affected_family_id") or "").strip()
            or not str(event.get("authorized_run_id") or "").strip()
            or not str(event.get("integrated_code_commit") or "").strip()
            or not str(event.get("operator_id") or "").strip()
        ):
            raise ValueError("an ambiguous continuation event changed")
        request_identity = event.get("request_identity")
        if (
            not isinstance(request_identity, dict)
            or set(request_identity)
            != {
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "request_sha256",
                "reserved_usd",
            }
            or not all(
                isinstance(value, str) and value for value in request_identity.values()
            )
        ):
            raise ValueError("an ambiguous continuation request identity changed")
        if (
            _money(event.get("reserved_usd"), "continuation reservation", positive=True)
            <= 0
        ):
            raise ValueError("an ambiguous continuation reservation changed")
        evidence_path = Path(str(event.get("evidence_file") or "")).resolve()
        review_path = Path(str(event.get("review_file") or "")).resolve()
        if (
            not evidence_path.is_file()
            or sha256_file(evidence_path) != event["evidence_file_sha256"]
            or not review_path.is_file()
            or sha256_file(review_path) != event["review_file_sha256"]
        ):
            raise ValueError("an ambiguous continuation evidence changed")
        evidence = _read(evidence_path)
        if (
            not isinstance(evidence, dict)
            or set(evidence)
            != {
                "schema",
                "request_key",
                "error_class",
                "http_status",
                "live_call_made",
                "received_receipt_absent",
                "actual_cost_known",
                "replay_prohibited",
                "affected_family_id",
                "authorized_run_id",
            }
            or evidence.get("schema") != AMBIGUOUS_CONTINUATION_EVIDENCE_SCHEMA
            or evidence.get("request_key") != request_key
            or evidence.get("error_class") != event["error_class"]
            or evidence.get("http_status") != event["http_status"]
            or evidence.get("live_call_made") is not True
            or evidence.get("received_receipt_absent") is not True
            or evidence.get("actual_cost_known") is not False
            or evidence.get("replay_prohibited") is not True
            or evidence.get("affected_family_id") != event["affected_family_id"]
            or evidence.get("authorized_run_id") != event["authorized_run_id"]
        ):
            raise ValueError("an ambiguous continuation evidence changed")
        try:
            authorized = datetime.fromisoformat(
                str(event["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("an ambiguous continuation time changed") from error
        if authorized.tzinfo is None:
            raise ValueError("an ambiguous continuation time changed")
        return event

    def _validate_received_max_tokens_continuation(
        self, event: dict[str, Any], path: Path
    ) -> dict[str, Any]:
        if set(event) != RECEIVED_MAX_TOKENS_CONTINUATION_FIELDS:
            raise ValueError("an ambiguous continuation event changed")
        request_key = event.get("request_key")
        if (
            not isinstance(request_key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_key)
            or path.name != f"ambiguous-continuation-{request_key}.json"
        ):
            raise ValueError("an ambiguous continuation event changed")
        for field in (
            "ambiguous_receipt_sha256",
            "received_receipt_sha256",
            "request_trace_sha256",
            "evidence_file_sha256",
            "review_file_sha256",
            "ledger_sha256_before",
            "gate_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(event.get(field) or "")):
                raise ValueError("an ambiguous continuation event changed")
        if (
            event.get("error_class") != "received_max_tokens_usage_unknown"
            or event.get("finish_reason") != "MAX_TOKENS"
            or event.get("live_call_made") is not True
            or event.get("received_receipt_present") is not True
            or event.get("reservation_policy")
            != AMBIGUOUS_CONTINUATION_RESERVATION_POLICY
            or event.get("scope") != "unrelated_families_only"
            or event.get("skip_reason_code")
            != "operational_ambiguous_charge_received_max_tokens"
            or not str(event.get("affected_family_id") or "").strip()
            or not str(event.get("authorized_run_id") or "").strip()
            or not str(event.get("integrated_code_commit") or "").strip()
            or not str(event.get("operator_id") or "").strip()
        ):
            raise ValueError("an ambiguous continuation event changed")
        request_identity = event.get("request_identity")
        if (
            not isinstance(request_identity, dict)
            or set(request_identity)
            != {
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "request_sha256",
                "reserved_usd",
            }
            or not all(
                isinstance(value, str) and value for value in request_identity.values()
            )
            or _money(
                event.get("reserved_usd"), "continuation reservation", positive=True
            )
            <= 0
        ):
            raise ValueError("an ambiguous continuation request identity changed")
        evidence_path = Path(str(event.get("evidence_file") or "")).resolve()
        review_path = Path(str(event.get("review_file") or "")).resolve()
        if (
            not evidence_path.is_file()
            or sha256_file(evidence_path) != event["evidence_file_sha256"]
            or not review_path.is_file()
            or sha256_file(review_path) != event["review_file_sha256"]
        ):
            raise ValueError("an ambiguous continuation evidence changed")
        evidence = _read(evidence_path)
        if evidence != {
            "schema": RECEIVED_MAX_TOKENS_CONTINUATION_EVIDENCE_SCHEMA,
            "request_key": request_key,
            "error_class": event["error_class"],
            "finish_reason": event["finish_reason"],
            "live_call_made": True,
            "received_receipt_present": True,
            "actual_cost_known": False,
            "replay_prohibited": True,
            "affected_family_id": event["affected_family_id"],
            "authorized_run_id": event["authorized_run_id"],
        }:
            raise ValueError("an ambiguous continuation evidence changed")
        try:
            authorized = datetime.fromisoformat(
                str(event["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("an ambiguous continuation time changed") from error
        if authorized.tzinfo is None:
            raise ValueError("an ambiguous continuation time changed")
        return event

    def _validate_provider_timeout_continuation(
        self, event: dict[str, Any], path: Path
    ) -> dict[str, Any]:
        if set(event) != PROVIDER_TIMEOUT_CONTINUATION_FIELDS:
            raise ValueError("an ambiguous continuation event changed")
        request_key = event.get("request_key")
        if (
            not isinstance(request_key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_key)
            or path.name != f"ambiguous-continuation-{request_key}.json"
        ):
            raise ValueError("an ambiguous continuation event changed")
        for field in (
            "ambiguous_receipt_sha256",
            "evidence_file_sha256",
            "review_file_sha256",
            "ledger_sha256_before",
            "gate_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(event.get(field) or "")):
                raise ValueError("an ambiguous continuation event changed")
        timeout_seconds = event.get("timeout_seconds")
        if (
            event.get("error_class") != PROVIDER_TIMEOUT_ERROR_CLASS
            or event.get("error") != PROVIDER_TIMEOUT_ERROR
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= MAXIMUM_CALL_TIMEOUT_SECONDS
            or event.get("live_call_made") is not True
            or event.get("received_receipt_absent") is not True
            or event.get("reservation_policy")
            != AMBIGUOUS_CONTINUATION_RESERVATION_POLICY
            or event.get("scope") != "unrelated_families_only"
            or event.get("skip_reason_code") != PROVIDER_TIMEOUT_SKIP_REASON
            or not str(event.get("affected_family_id") or "").strip()
            or not str(event.get("authorized_run_id") or "").strip()
            or not str(event.get("integrated_code_commit") or "").strip()
            or not str(event.get("operator_id") or "").strip()
        ):
            raise ValueError("an ambiguous continuation event changed")
        request_identity = event.get("request_identity")
        if (
            not isinstance(request_identity, dict)
            or set(request_identity)
            != {
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "request_sha256",
                "reserved_usd",
            }
            or not all(
                isinstance(value, str) and value for value in request_identity.values()
            )
            or _money(
                event.get("reserved_usd"), "continuation reservation", positive=True
            )
            <= 0
        ):
            raise ValueError("an ambiguous continuation request identity changed")
        evidence_path = Path(str(event.get("evidence_file") or "")).resolve()
        review_path = Path(str(event.get("review_file") or "")).resolve()
        if (
            not evidence_path.is_file()
            or sha256_file(evidence_path) != event["evidence_file_sha256"]
            or not review_path.is_file()
            or sha256_file(review_path) != event["review_file_sha256"]
        ):
            raise ValueError("an ambiguous continuation evidence changed")
        if _read(evidence_path) != {
            "schema": PROVIDER_TIMEOUT_CONTINUATION_EVIDENCE_SCHEMA,
            "request_key": request_key,
            "error_class": PROVIDER_TIMEOUT_ERROR_CLASS,
            "error": PROVIDER_TIMEOUT_ERROR,
            "timeout_seconds": timeout_seconds,
            "live_call_made": True,
            "received_receipt_absent": True,
            "actual_cost_known": False,
            "replay_prohibited": True,
            "affected_family_id": event["affected_family_id"],
            "authorized_run_id": event["authorized_run_id"],
        }:
            raise ValueError("an ambiguous continuation evidence changed")
        try:
            authorized = datetime.fromisoformat(
                str(event["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("an ambiguous continuation time changed") from error
        if authorized.tzinfo is None:
            raise ValueError("an ambiguous continuation time changed")
        return event

    def _ambiguous_continuation_events(
        self, ledger: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        for path in self._receipt_paths(
            prefix="ambiguous-continuation-", suffix=".json"
        ):
            event = self._read_ambiguous_continuation(path)
            request_key = event["request_key"]
            if request_key in events or request_key not in ledger["requests"]:
                raise ValueError("an ambiguous continuation request changed")
            request = ledger["requests"][request_key]
            if (
                request.get("state") != "ambiguous_charge"
                or event["affected_family_id"] != request.get("family_id")
                or event["authorized_run_id"] != request.get("run_id")
                or event["request_identity"]
                != {
                    key: request.get(key)
                    for key in (
                        "run_id",
                        "stage",
                        "paper_id",
                        "family_id",
                        "source_version_id",
                        "request_sha256",
                        "reserved_usd",
                    )
                }
                or event["reserved_usd"] != request.get("reserved_usd")
            ):
                raise ValueError("an ambiguous continuation request identity changed")
            final_path = self.receipts_dir / f"{request_key}.json"
            received_path = self.receipts_dir / f"{request_key}.received.json"
            if not final_path.is_file() or event[
                "ambiguous_receipt_sha256"
            ] != sha256_file(final_path):
                raise ValueError("an ambiguous continuation receipt changed")
            final = _read(final_path)
            if event["schema"] == AMBIGUOUS_CONTINUATION_SCHEMA:
                if (
                    received_path.exists()
                    or final.get("state") != "ambiguous_charge"
                    or final.get("error_class") != event["error_class"]
                    or final.get("http_status") != event["http_status"]
                    or final.get("live_call_made") is not True
                    or "response" in final
                ):
                    raise ValueError("an ambiguous continuation receipt changed")
            elif event["schema"] == PROVIDER_TIMEOUT_CONTINUATION_SCHEMA:
                if (
                    not _is_provider_timeout_ambiguous_case(
                        final,
                        request,
                        received_receipt_present=received_path.exists(),
                    )
                    or _receipt_timeout_seconds(final) != event["timeout_seconds"]
                ):
                    raise ValueError("an ambiguous continuation receipt changed")
            else:
                trace_path = self.receipts_dir / f"{request_key}.request-trace.json"
                if (
                    not received_path.is_file()
                    or not trace_path.is_file()
                    or event["received_receipt_sha256"] != sha256_file(received_path)
                    or event["request_trace_sha256"] != sha256_file(trace_path)
                    or not _is_received_max_tokens_ambiguous_case(
                        final,
                        _read(received_path),
                        _read(trace_path),
                        request,
                    )
                ):
                    raise ValueError("an ambiguous continuation receipt changed")
            events[request_key] = event
        return events

    def _read_orphaned_continuation(self, path: Path) -> dict[str, Any]:
        event = _read(path)
        if (
            not isinstance(event, dict)
            or set(event) != ORPHANED_CONTINUATION_FIELDS
            or event.get("schema") != ORPHANED_CONTINUATION_SCHEMA
        ):
            raise ValueError("an orphaned continuation event changed")
        request_key = event.get("request_key")
        if (
            not isinstance(request_key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_key)
            or path.name != f"orphaned-continuation-{request_key}.json"
        ):
            raise ValueError("an orphaned continuation event changed")
        for field in (
            "submitted_receipt_sha256",
            "request_trace_sha256",
            "evidence_file_sha256",
            "review_file_sha256",
            "ledger_sha256_before",
            "gate_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(event.get(field) or "")):
                raise ValueError("an orphaned continuation event changed")
        if (
            event.get("reservation_policy") != ORPHANED_CONTINUATION_RESERVATION_POLICY
            or event.get("scope") != "unrelated_families_only"
            or event.get("skip_reason_code") != "operational_orphaned_request_no_replay"
            or not str(event.get("affected_family_id") or "").strip()
            or not str(event.get("authorized_run_id") or "").strip()
            or not str(event.get("integrated_code_commit") or "").strip()
            or not str(event.get("operator_id") or "").strip()
        ):
            raise ValueError("an orphaned continuation event changed")
        identity = event.get("request_identity")
        if not isinstance(identity, dict) or set(identity) != {
            "run_id",
            "stage",
            "paper_id",
            "family_id",
            "source_version_id",
            "request_sha256",
            "reserved_usd",
            "submitted_at_utc",
        }:
            raise ValueError("an orphaned continuation event changed")
        try:
            authorized = datetime.fromisoformat(
                str(event["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("an orphaned continuation time changed") from error
        if authorized.tzinfo is None:
            raise ValueError("an orphaned continuation time changed")
        return event

    def _orphaned_continuation_events(
        self, ledger: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        for path in self._receipt_paths(
            prefix="orphaned-continuation-", suffix=".json"
        ):
            event = self._read_orphaned_continuation(path)
            request_key = event["request_key"]
            request = ledger["requests"].get(request_key)
            if (
                request_key in events
                or request is None
                or request.get("state") != "orphaned_no_replay"
            ):
                raise ValueError("an orphaned continuation request changed")
            if any(
                event["request_identity"].get(field) != request.get(field)
                for field in event["request_identity"]
            ) or event["affected_family_id"] != request.get("family_id"):
                raise ValueError("an orphaned continuation request changed")
            event_stem = self._request_event_stem(request_key, request)
            submitted_path = self.receipts_dir / f"{event_stem}.submitted.json"
            trace_path = self.receipts_dir / f"{request_key}.request-trace.json"
            if (
                not submitted_path.is_file()
                or not trace_path.is_file()
                or sha256_file(submitted_path) != event["submitted_receipt_sha256"]
                or sha256_file(trace_path) != event["request_trace_sha256"]
                or request.get("orphaned_continuation_sha256") != sha256_file(path)
            ):
                raise ValueError("an orphaned continuation custody changed")
            evidence_path = Path(event["evidence_file"])
            review_path = Path(event["review_file"])
            if (
                not evidence_path.is_file()
                or not review_path.is_file()
                or sha256_file(evidence_path) != event["evidence_file_sha256"]
                or sha256_file(review_path) != event["review_file_sha256"]
            ):
                raise ValueError("an orphaned continuation review evidence changed")
            events[request_key] = event
        if {
            key
            for key, request in ledger["requests"].items()
            if request.get("state") == "orphaned_no_replay"
        } != set(events):
            raise ValueError("an orphaned request lacks its continuation event")
        return events

    # The ledger's own consistency is proved on every read and every commit.
    # The proof used to loop over every request row, which was 150 ms at 6,036
    # rows and, at five reads and three commits a paid call, about a second of
    # the serialised admission. The proof is now factored into three parts:
    # the shape of the ledger, the contribution of one request row, and the
    # comparison of the summed contributions with what the ledger stores. A
    # full pass sums every row. The hot path re-sums only the rows a mutation
    # touched, and the background compactor runs a full pass off the hot path.

    _SUBMITTED_STATES = frozenset(
        {"submitted", "orphaned_no_replay", "completed", "ambiguous_charge"}
    )
    _TERMINAL_STATES = frozenset(
        {
            "completed",
            "ambiguous_charge",
            "count_error",
            "too_large_not_ready",
            "not_submitted",
            "orphaned_no_replay",
        }
    )

    def _validate_ledger_shape(self, ledger: dict[str, Any]) -> None:
        required = {
            "schema",
            "policy_sha256",
            "price_config_sha256",
            "prior_construction_spend_usd",
            "reserved_usd",
            "spent_usd",
            "ambiguous_reserved_usd",
            "generation_submissions",
            "count_requests",
            "inflight",
            "accepted_question_count",
            "accepted_families",
            "family_bindings",
            "paper_bindings",
            "live_test_papers",
            "recent_submission_times_utc",
            "requests",
            "stages",
            "papers",
            "halted",
            "halt_reason",
            "created_at_utc",
            "updated_at_utc",
        }
        if (
            not isinstance(ledger, dict)
            or set(ledger) - set(EVALUATION_HALT_FIELDS) != required
        ):
            raise ValueError("the shared paid-call ledger fields changed")
        if ledger.get("evaluation_halted") is not None and not isinstance(
            ledger["evaluation_halted"], bool
        ):
            raise ValueError("the benchmark evaluation halt state is invalid")
        if ledger["schema"] != "shared-paid-call-ledger-v1":
            raise ValueError("the shared paid-call ledger schema changed")
        if (
            not isinstance(ledger["requests"], dict)
            or not isinstance(ledger["family_bindings"], dict)
            or not isinstance(ledger["paper_bindings"], dict)
        ):
            raise ValueError("the shared paid-call ledger mappings are invalid")
        if not isinstance(ledger["halted"], bool):
            raise ValueError("the shared paid-call halt state is invalid")

    def _row_contribution(
        self, ledger: dict[str, Any], key: str, request: Any
    ) -> dict[str, Any] | None:
        """Validate one request row and return what it adds to the totals.

        ``None`` means the row is valid and adds nothing, which is every row
        that was never submitted.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(request, dict):
            raise ValueError("the shared paid-call request identity is invalid")
        if request.get("request_key") != key:
            raise ValueError("a paid-call request key does not match its record")
        for name in (
            "request_sha256",
            "run_id",
            "stage",
            "paper_id",
            "family_id",
            "source_version_id",
            "model",
            "gate_sha256",
        ):
            if not isinstance(request.get(name), str) or not request[name]:
                raise ValueError(f"a paid-call request lacks {name}")
        request_config_hash = request.get("price_config_sha256")
        if request_config_hash is not None and (
            not isinstance(request_config_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_config_hash)
        ):
            raise ValueError("a paid-call request has an invalid price config hash")
        request_policy_hash = request.get("policy_sha256")
        if request_policy_hash is not None and (
            not isinstance(request_policy_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_policy_hash)
        ):
            raise ValueError("a paid-call request has an invalid policy hash")
        transition_hash = request.get("config_transition_sha256")
        if transition_hash is not None and (
            not isinstance(transition_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", transition_hash)
        ):
            raise ValueError(
                "a paid-call request has an invalid config transition hash"
            )
        resume_receipt_sha256 = request.get("resumed_from_not_submitted_sha256")
        resume_transition_sha256 = request.get("resumed_from_config_transition_sha256")
        if (resume_receipt_sha256 is None) != (resume_transition_sha256 is None):
            raise ValueError("a paid-call resume binding is incomplete")
        for value in (resume_receipt_sha256, resume_transition_sha256):
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
            ):
                raise ValueError("a paid-call resume binding is invalid")
        if resume_transition_sha256 is not None and (
            transition_hash is None or transition_hash == resume_transition_sha256
        ):
            raise ValueError("a paid-call resume transition did not advance")
        if not stage_supported(request["stage"]):
            raise ValueError("a paid-call request has an unsupported stage")
        binding = ledger["family_bindings"].get(request["family_id"])
        if binding != {
            "paper_id": request["paper_id"],
            "source_version_id": request["source_version_id"],
        }:
            raise ValueError("a paid-call family binding is inconsistent")
        state = request.get("state")
        if state not in {"counting", *self._TERMINAL_STATES, "submitted"}:
            raise ValueError("a paid-call request state is invalid")
        if state not in self._SUBMITTED_STATES:
            return None
        phase = request.get("phase")
        if phase not in PHASES:
            raise ValueError("a submitted paid-call phase is invalid")
        reserved = _money(
            request.get("reserved_usd"), "request reservation", positive=True
        )
        contribution = {
            "stage": request["stage"],
            "family_id": request["family_id"],
            "paper_id": request["paper_id"],
            "source_version_id": request["source_version_id"],
            "live": phase == "live_test",
            "inflight": 1 if state == "submitted" else 0,
            "reserved": Decimal("0"),
            "spent": Decimal("0"),
            "ambiguous": Decimal("0"),
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
        }
        if state in {"submitted", "orphaned_no_replay"}:
            contribution["reserved"] = reserved
        elif state == "ambiguous_charge":
            contribution["ambiguous"] = reserved
        else:
            actual = _money(request.get("actual_cost_usd"), "request actual cost")
            if actual > reserved:
                raise ValueError("a paid-call actual cost exceeds its reservation")
            usage = request.get("usage")
            if not isinstance(usage, dict):
                raise ValueError("a completed paid-call request lacks usage")
            for field in (
                "promptTokenCount",
                "candidatesTokenCount",
                "thoughtsTokenCount",
            ):
                value = usage.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("a completed paid-call request has invalid usage")
            contribution["spent"] = actual
            contribution["input_tokens"] = usage["promptTokenCount"]
            contribution["output_tokens"] = usage["candidatesTokenCount"]
            contribution["thinking_tokens"] = usage["thoughtsTokenCount"]
        return contribution

    @staticmethod
    def _empty_aggregate() -> dict[str, Any]:
        return {
            "stages": {},
            "papers": {},
            "live": {},
            "reserved": Decimal("0"),
            "spent": Decimal("0"),
            "ambiguous": Decimal("0"),
            "submissions": 0,
            "inflight": 0,
        }

    def _aggregate_apply(
        self, aggregate: dict[str, Any], contribution: dict[str, Any], sign: int
    ) -> None:
        """Add or subtract one row's contribution to the running totals."""
        aggregate["submissions"] += sign
        aggregate["inflight"] += sign * int(contribution["inflight"])
        for name in ("reserved", "spent", "ambiguous"):
            aggregate[name] += sign * contribution[name]
        stage = aggregate["stages"].setdefault(
            contribution["stage"],
            {
                "submissions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "reserved": Decimal("0"),
                "spent": Decimal("0"),
                "ambiguous": Decimal("0"),
            },
        )
        paper = aggregate["papers"].setdefault(
            contribution["family_id"],
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "reserved": Decimal("0"),
                "spent": Decimal("0"),
                "ambiguous": Decimal("0"),
                "paper_id": contribution["paper_id"],
                "source_version_id": contribution["source_version_id"],
            },
        )
        stage["submissions"] += sign
        for row in (stage, paper):
            for name in ("reserved", "spent", "ambiguous"):
                row[name] += sign * contribution[name]
            for name in ("input_tokens", "output_tokens", "thinking_tokens"):
                row[name] += sign * int(contribution[name])
        if contribution["live"]:
            live = aggregate["live"].setdefault(
                contribution["family_id"],
                {
                    "submissions": 0,
                    "reserved": Decimal("0"),
                    "spent": Decimal("0"),
                    "ambiguous": Decimal("0"),
                },
            )
            live["submissions"] += sign
            for name in ("reserved", "spent", "ambiguous"):
                live[name] += sign * contribution[name]
        if sign < 0:
            # A row that gave back everything it gave leaves no row behind,
            # exactly as a full pass would never have created one.
            if stage["submissions"] == 0:
                aggregate["stages"].pop(contribution["stage"], None)
            if all(
                paper[name] == 0
                for name in (
                    "reserved",
                    "spent",
                    "ambiguous",
                    "input_tokens",
                    "output_tokens",
                    "thinking_tokens",
                )
            ) and not any(
                other["family_id"] == contribution["family_id"]
                for other in self._row_contributions.values()
            ):
                aggregate["papers"].pop(contribution["family_id"], None)
            if contribution["live"]:
                live_row = aggregate["live"].get(contribution["family_id"])
                if live_row is not None and live_row["submissions"] == 0:
                    aggregate["live"].pop(contribution["family_id"], None)

    def _compare_ledger(
        self, ledger: dict[str, Any], aggregate: dict[str, Any]
    ) -> None:
        """Compare what the rows add up to with what the ledger stores."""
        source_bindings: dict[str, str] = {}
        expected_paper_bindings: dict[str, dict[str, str]] = {}
        for family_id, binding in ledger["family_bindings"].items():
            if (
                not isinstance(family_id, str)
                or not family_id
                or not isinstance(binding, dict)
                or set(binding) != {"paper_id", "source_version_id"}
                or not all(
                    isinstance(value, str) and value for value in binding.values()
                )
            ):
                raise ValueError("a paid-call family binding is invalid")
            previous = source_bindings.setdefault(
                binding["source_version_id"], family_id
            )
            if previous != family_id:
                raise ValueError(
                    "one source version is bound to multiple paper families"
                )
            paper_binding = {
                "family_id": family_id,
                "source_version_id": binding["source_version_id"],
            }
            previous_paper = expected_paper_bindings.setdefault(
                binding["paper_id"], paper_binding
            )
            if previous_paper != paper_binding:
                raise ValueError("one paper ID is bound to multiple paper families")
        if ledger["paper_bindings"] != expected_paper_bindings:
            raise ValueError("the paid-call paper bindings are inconsistent")
        for name, value in {
            "reserved_usd": aggregate["reserved"],
            "spent_usd": aggregate["spent"],
            "ambiguous_reserved_usd": aggregate["ambiguous"],
        }.items():
            if _money(ledger.get(name), name) != value:
                raise ValueError(f"the shared paid-call {name} total is inconsistent")
        for name, value in {
            "generation_submissions": aggregate["submissions"],
            "count_requests": len(ledger["requests"]),
            "inflight": aggregate["inflight"],
            "accepted_question_count": len(ledger["accepted_families"]),
        }.items():
            if ledger.get(name) != value:
                raise ValueError(f"the shared paid-call {name} total is inconsistent")

        def rows_match(actual: Any, expected: dict[str, dict[str, Any]]) -> bool:
            if not isinstance(actual, dict) or set(actual) != set(expected):
                return False
            for identity, expected_row in expected.items():
                actual_row = actual.get(identity)
                if not isinstance(actual_row, dict) or set(actual_row) != set(
                    expected_row
                ):
                    return False
                for name, value in expected_row.items():
                    actual_value = actual_row.get(name)
                    if actual_value == value:
                        # The expected money is ``str`` of the Decimal the rows
                        # add up to, so equal strings are equal money. The
                        # comparison walks every stage, family and live-test
                        # row on every proof of one moved row, and parsing two
                        # Decimals for each unchanged field was most of it.
                        continue
                    if not name.endswith("_usd"):
                        return False
                    if _money(actual_value, name) != _money(value, name):
                        return False
            return True

        expected_stages = {
            stage: {
                "submissions": row["submissions"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "thinking_tokens": row["thinking_tokens"],
                "reserved_usd": str(row["reserved"]),
                "spent_usd": str(row["spent"]),
                "ambiguous_usd": str(row["ambiguous"]),
            }
            for stage, row in aggregate["stages"].items()
        }
        expected_papers = {
            family_id: {
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "thinking_tokens": row["thinking_tokens"],
                "reserved_usd": str(row["reserved"]),
                "spent_usd": str(row["spent"]),
                "ambiguous_usd": str(row["ambiguous"]),
                "paper_id": row["paper_id"],
                "source_version_id": row["source_version_id"],
            }
            for family_id, row in aggregate["papers"].items()
        }
        expected_live = {
            family_id: {
                "submissions": row["submissions"],
                "reserved_usd": str(row["reserved"]),
                "spent_usd": str(row["spent"]),
                "ambiguous_usd": str(row["ambiguous"]),
            }
            for family_id, row in aggregate["live"].items()
        }
        for name, value in {
            "stages": expected_stages,
            "papers": expected_papers,
            "live_test_papers": expected_live,
        }.items():
            if not rows_match(ledger.get(name), value):
                raise ValueError(f"the shared paid-call {name} total is inconsistent")
        if (
            not isinstance(ledger["recent_submission_times_utc"], list)
            or len(ledger["recent_submission_times_utc"]) > aggregate["submissions"]
        ):
            raise ValueError("the paid-call submission window is inconsistent")
        for value in ledger["recent_submission_times_utc"]:
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _prove_ledger(
        self, ledger: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Prove the whole ledger, row by row, and keep nothing.

        This is the full pass. It holds no state of its own, so the compactor
        thread runs it against its own materialization while the hot path runs
        its incremental proof against the store.
        """
        self._validate_ledger_shape(ledger)
        aggregate = self._empty_aggregate()
        contributions: dict[str, dict[str, Any]] = {}
        for key, request in ledger["requests"].items():
            contribution = self._row_contribution(ledger, key, request)
            if contribution is None:
                continue
            contributions[key] = contribution
            self._aggregate_apply(aggregate, contribution, 1)
        self._compare_ledger(ledger, aggregate)
        return aggregate, contributions

    def _validate_ledger(self, ledger: dict[str, Any]) -> None:
        """Prove the whole ledger and keep the proof for the next mutation."""
        aggregate, contributions = self._prove_ledger(ledger)
        self._row_contributions = contributions
        self._ledger_aggregate = aggregate
        self._aggregate_state_id = id(ledger)

    def _validate_ledger_delta(
        self, ledger: dict[str, Any], changed: Iterable[str]
    ) -> None:
        """Prove a ledger whose rows moved since the last proof.

        Only the rows the mutation touched are summed again. Everything the
        full pass compares is compared again, because the comparison is of
        the whole ledger and costs the size of the stage, paper and binding
        maps, not the size of the history.
        """
        if (
            self._aggregate_state_id != id(ledger)
            or self._ledger_aggregate is None
            or self._row_contributions is None
        ):
            self._validate_ledger(ledger)
            return
        self._validate_ledger_shape(ledger)
        aggregate = self._ledger_aggregate
        contributions = self._row_contributions
        for key in changed:
            previous = contributions.pop(key, None)
            if previous is not None:
                self._aggregate_apply(aggregate, previous, -1)
        for key in changed:
            request = ledger["requests"].get(key)
            if request is None:
                continue
            contribution = self._row_contribution(ledger, key, request)
            if contribution is None:
                continue
            contributions[key] = contribution
            self._aggregate_apply(aggregate, contribution, 1)
        self._compare_ledger(ledger, aggregate)

    @staticmethod
    def _phase_halted(ledger: dict[str, Any], phase: str) -> str | None:
        """Return the halt reason that blocks one phase, or None."""
        return phase_halt_reason(ledger, phase)

    def _lift_settled_halts(self, ledger: dict[str, Any]) -> None:
        """Lift each halt whose ambiguous requests are all settled.

        An ambiguous request with a reviewed continuation event is settled for
        this purpose, which is the rule the reservation path already applies.
        """
        continuations = self._ambiguous_continuation_events(ledger)
        blocking = [
            row
            for key, row in ledger["requests"].items()
            if row.get("state") == "ambiguous_charge" and key not in continuations
        ]
        if not blocking and int(ledger["inflight"]) == 0:
            ledger["halted"] = False
            ledger["halt_reason"] = None
        if not any(row.get("phase") == EVALUATION_PHASE for row in blocking):
            if ledger.get("evaluation_halted"):
                ledger["evaluation_halted"] = False
                ledger["evaluation_halt_reason"] = None

    @classmethod
    def _phase_inflight(cls, ledger: dict[str, Any], phase: str) -> int:
        """Count the in-flight requests of one phase.

        The ledger counter covers every phase. The evaluation phase and the
        construction phases each keep their own slots, so an evaluation request
        in flight never takes a construction slot, and the reverse. The
        per-minute window stays shared: a collision there is transient, and
        ``execute`` waits for it.
        """
        evaluation_inflight = int(cls._evaluation_totals(ledger)["inflight"])
        if phase == EVALUATION_PHASE:
            return evaluation_inflight
        return max(int(ledger["inflight"]) - evaluation_inflight, 0)

    @staticmethod
    def _evaluation_totals(ledger: dict[str, Any]) -> dict[str, Any]:
        """Sum the evaluation-phase liabilities recorded in the ledger requests."""
        totals: dict[str, Any] = {
            "reserved_usd": Decimal("0"),
            "spent_usd": Decimal("0"),
            "ambiguous_usd": Decimal("0"),
            "submissions": 0,
            "inflight": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "calls_by_trial_key": {},
        }
        for request in ledger["requests"].values():
            if request.get("phase") != EVALUATION_PHASE:
                continue
            state = request.get("state")
            if state not in {
                "submitted",
                "orphaned_no_replay",
                "completed",
                "ambiguous_charge",
            }:
                continue
            reserved = _money(request.get("reserved_usd"), "evaluation reservation")
            totals["submissions"] += 1
            trial = request.get("evaluation_trial") or {}
            trial_key = canonical_json(
                {
                    "family_id": request.get("family_id"),
                    "stage": request.get("stage"),
                    "condition": trial.get("condition"),
                    "arm": trial.get("arm"),
                }
            )
            totals["calls_by_trial_key"][trial_key] = (
                totals["calls_by_trial_key"].get(trial_key, 0) + 1
            )
            if state in {"submitted", "orphaned_no_replay"}:
                totals["reserved_usd"] += reserved
                if state == "submitted":
                    totals["inflight"] += 1
            elif state == "ambiguous_charge":
                totals["ambiguous_usd"] += reserved
            else:
                totals["spent_usd"] += _money(
                    request.get("actual_cost_usd"), "evaluation actual cost"
                )
                usage = request.get("usage") or {}
                totals["input_tokens"] += int(usage.get("promptTokenCount", 0))
                totals["output_tokens"] += int(usage.get("candidatesTokenCount", 0))
                totals["thinking_tokens"] += int(usage.get("thoughtsTokenCount", 0))
        totals["used_usd"] = (
            totals["reserved_usd"] + totals["spent_usd"] + totals["ambiguous_usd"]
        )
        return totals

    def _status_payload(self, ledger: dict[str, Any]) -> dict[str, Any]:
        evaluation = self._evaluation_totals(ledger)
        away_used = (
            sum(
                _money(ledger[name], name)
                for name in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
            )
            - evaluation["used_usd"]
        )
        live_used = sum(
            _money(row.get("reserved_usd", 0), "live reserved")
            + _money(row.get("spent_usd", 0), "live spent")
            + _money(row.get("ambiguous_usd", 0), "live ambiguous")
            for row in ledger["live_test_papers"].values()
        )
        live_test_cap = _money(self.policy["live_test_suballocation_usd"], "live test")
        if self._authorized_live_test_ceiling_usd is not None:
            live_test_cap = min(live_test_cap, self._authorized_live_test_ceiling_usd)
        construction_used = self.prior + away_used
        lifetime_used = construction_used + evaluation["used_usd"]
        construction_submissions = (
            int(ledger["generation_submissions"]) - evaluation["submissions"]
        )
        accepted = int(ledger["accepted_question_count"])
        cutoff = datetime.now(UTC) - timedelta(minutes=1)
        recent_count = sum(
            datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
            for value in ledger["recent_submission_times_utc"]
        )
        live_submissions = sum(
            int(row.get("submissions", 0))
            for row in ledger["live_test_papers"].values()
        )
        papers = {}
        paper_cap = _money(self.policy["maximum_paper_cost_usd"], "paper cap")
        for family_id, row in ledger["papers"].items():
            used = sum(
                _money(row[name], f"paper {name}")
                for name in ("reserved_usd", "spent_usd", "ambiguous_usd")
            )
            papers[family_id] = {**row, "remaining_usd": str(paper_cap - used)}
        limits = {
            key: self.policy[key]
            for key in (
                "project_lifetime_ceiling_usd",
                "reserved_for_benchmark_evaluation_usd",
                "dataset_construction_allocation_usd",
                "construction_review_checkpoint_usd",
                "accepted_question_target",
                "away_session_total_ceiling_usd",
                "live_test_suballocation_usd",
                "live_test_maximum_papers",
                "live_test_maximum_generation_submissions",
                "away_maximum_generation_submissions",
                "maximum_request_reserved_cost_usd",
                "maximum_paper_cost_usd",
                "maximum_concurrent_generation_requests",
                "maximum_generation_requests_per_minute",
                "maximum_output_tokens_including_thinking",
                "automatic_transport_generation_retries",
            )
        }
        limits["authorized_live_test_ceiling_usd"] = (
            str(self._authorized_live_test_ceiling_usd)
            if self._authorized_live_test_ceiling_usd is not None
            else None
        )
        usage = {
            "project_lifetime_usd": str(lifetime_used),
            "benchmark_evaluation_usd": str(evaluation["used_usd"]),
            "dataset_construction_usd": str(construction_used),
            "construction_checkpoint_usd": str(construction_used),
            "away_session_usd": str(away_used),
            "live_test_usd": str(live_used),
            "accepted_questions": accepted,
            "live_test_papers": len(ledger["live_test_papers"]),
            "live_test_generation_submissions": live_submissions,
            "away_generation_submissions": construction_submissions,
            "concurrent_generation_requests": self._phase_inflight(
                ledger, "away_production"
            ),
            "generation_requests_in_current_minute": recent_count,
        }
        remaining = {
            "project_lifetime_usd": str(
                _money(self.policy["project_lifetime_ceiling_usd"], "lifetime")
                - lifetime_used
            ),
            "benchmark_evaluation_usd": str(
                _money(
                    self.policy["reserved_for_benchmark_evaluation_usd"],
                    "evaluation reserve",
                )
                - evaluation["used_usd"]
            ),
            "dataset_construction_usd": str(
                _money(
                    self.policy["dataset_construction_allocation_usd"],
                    "construction allocation",
                )
                - construction_used
            ),
            "construction_checkpoint_usd": str(
                _money(self.policy["construction_review_checkpoint_usd"], "checkpoint")
                - construction_used
            ),
            "away_session_usd": str(
                _money(self.policy["away_session_total_ceiling_usd"], "away")
                - away_used
            ),
            "live_test_usd": str(live_test_cap - live_used),
            "accepted_questions": int(self.policy["accepted_question_target"])
            - accepted,
            "live_test_papers": (
                None
                if self.policy["live_test_maximum_papers"] is None
                else int(self.policy["live_test_maximum_papers"])
                - len(ledger["live_test_papers"])
            ),
            "live_test_generation_submissions": (
                None
                if self.policy["live_test_maximum_generation_submissions"] is None
                else int(self.policy["live_test_maximum_generation_submissions"])
                - live_submissions
            ),
            "away_generation_submissions": int(
                self.policy["away_maximum_generation_submissions"]
            )
            - construction_submissions,
            "concurrent_generation_requests": int(
                self.policy["maximum_concurrent_generation_requests"]
            )
            - self._phase_inflight(ledger, "away_production"),
            "generation_requests_in_current_minute": int(
                self.policy["maximum_generation_requests_per_minute"]
            )
            - recent_count,
        }
        return {
            "schema": "shared-gemini-broker-status-v2",
            "ledger_file": str(self.ledger_file),
            "ledger_sha256": sha256_file(self.ledger_file),
            "policy_sha256": sha256_file(self.policy_file),
            "initial_policy_sha256": ledger["policy_sha256"],
            "price_config_sha256": self.active_price_config_sha256,
            "initial_price_config_sha256": ledger["price_config_sha256"],
            "config_transition_sha256": self._config_transition_sha256,
            "updated_at_utc": ledger["updated_at_utc"],
            "spent_usd": ledger["spent_usd"],
            "reserved_usd": ledger["reserved_usd"],
            "ambiguous_reserved_usd": ledger["ambiguous_reserved_usd"],
            "away_remaining_usd": remaining["away_session_usd"],
            "live_test_remaining_usd": remaining["live_test_usd"],
            "construction_checkpoint_remaining_usd": remaining[
                "construction_checkpoint_usd"
            ],
            "cost_per_accepted_question_usd": (
                str(_money(ledger["spent_usd"], "spent") / accepted)
                if accepted
                else None
            ),
            "generation_submissions": ledger["generation_submissions"],
            "count_requests": ledger["count_requests"],
            "inflight": ledger["inflight"],
            "accepted_question_count": accepted,
            "halted": ledger["halted"],
            "halt_reason": ledger["halt_reason"],
            "integrity_valid": True,
            "status_state": "valid",
            "limits": limits,
            "usage": usage,
            "remaining": remaining,
            "evaluation": self._evaluation_status(evaluation, ledger),
            "stages": ledger["stages"],
            "papers": papers,
        }

    def _evaluation_status(
        self, evaluation: dict[str, Any], ledger: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Report the evaluation phase beside, never inside, construction totals."""
        policy = self.evaluation_policy
        ceiling = (
            _money(policy["evaluation_ceiling_usd"], "evaluation ceiling")
            if policy
            else None
        )
        return {
            "phase": EVALUATION_PHASE,
            "policy_id": policy["policy_id"] if policy else None,
            "policy_sha256": (
                sha256_file(self.evaluation_policy_file)
                if self.evaluation_policy_file
                else None
            ),
            "price_config_sha256": self.active_evaluation_price_config_sha256,
            "gate_sha256": (
                sha256_file(self.evaluation_gate_file)
                if self.evaluation_gate_file
                else None
            ),
            "ceiling_usd": str(ceiling) if ceiling is not None else None,
            "reserved_usd": str(evaluation["reserved_usd"]),
            "spent_usd": str(evaluation["spent_usd"]),
            "ambiguous_usd": str(evaluation["ambiguous_usd"]),
            "used_usd": str(evaluation["used_usd"]),
            "remaining_usd": (
                str(ceiling - evaluation["used_usd"]) if ceiling is not None else None
            ),
            "halted": bool(
                (ledger or {}).get("evaluation_halted") or (ledger or {}).get("halted")
            ),
            "halt_reason": (
                str((ledger or {}).get("halt_reason"))
                if (ledger or {}).get("halted")
                else (
                    str((ledger or {}).get("evaluation_halt_reason"))
                    if (ledger or {}).get("evaluation_halted")
                    else None
                )
            ),
            "phase_halted": bool((ledger or {}).get("evaluation_halted")),
            "submissions": evaluation["submissions"],
            "inflight": evaluation["inflight"],
            "input_tokens": evaluation["input_tokens"],
            "output_tokens": evaluation["output_tokens"],
            "thinking_tokens": evaluation["thinking_tokens"],
        }

    def _start_compactor(self) -> None:
        """Start the one background thread that keeps the snapshot current."""
        if self._compaction_thread is not None:
            return
        # A process that exits writes the snapshot one last time, so a plain
        # reader of the file after the exit sees the state and not the last
        # compaction tick. A signal that kills the process outright gets no
        # such write; the activation's stop compacts for it.
        atexit.register(self._compact_at_exit)
        thread = threading.Thread(
            target=self._compaction_loop,
            name="shared-ledger-compactor",
            daemon=True,
        )
        self._compaction_thread = thread
        thread.start()

    def _compaction_loop(self) -> None:
        while not self._compaction_stop.wait(ledger_store.COMPACTION_INTERVAL_SECONDS):
            try:
                self.compact()
            except Exception as error:  # pragma: no cover - defensive
                self._record_integrity_halt(error)
                self._publish_integrity_halt(error)
                return

    def compact(self, *, wait: bool = False) -> bool:
        """Rewrite the snapshot and the status file, and prove the whole ledger.

        Nothing here is on the path of a paid call. The compactor never takes
        the shared ledger lock: it materializes the store from the snapshot and
        the journal, which are both safe to read while another process appends,
        and it writes the snapshot with an atomic rename. It also runs the full
        row-by-row proof, which the hot path no longer runs on every read.

        ``wait`` is for a start and for a reviewed operation, which are not
        hot paths and must not skip the snapshot because another compactor
        held the lock at that instant: a reviewed operation binds the file.
        """
        with ledger_store.held_compaction_lock(self.ledger_file, wait=wait) as held:
            if not held:
                return False
            state, seq, offset = ledger_store.materialize(self.ledger_file)
            if seq <= self._compacted_seq:
                return False
            self._prove_ledger(state)
            ledger_store.write_snapshot(
                self.ledger_file, state, seq, journal_offset=offset
            )
        self._compacted_seq = seq
        self._publish_status(state)
        return True

    def _compact_at_exit(self) -> None:
        try:
            self.stop_compactor()
        except Exception:  # pragma: no cover - the process is leaving
            pass

    def stop_compactor(self) -> None:
        """Stop the background compactor after one last compaction."""
        self._compaction_stop.set()
        thread = self._compaction_thread
        if thread is not None:
            thread.join(timeout=30.0)
            self._compaction_thread = None
        with self._ledger_lock():
            pass
        self.compact(wait=True)

    def _publish_status(self, ledger: dict[str, Any]) -> None:
        atomic_json(self._status_file, self._status_payload(ledger))
        if self._status_observer is not None:
            self._status_observer(self._status_file)

    def set_status_observer(self, observer: Callable[[Path], None] | None) -> None:
        """Refresh a derived custody record after each durable broker state."""
        self._status_observer = observer
        if observer is not None:
            observer(self._status_file)

    @staticmethod
    def _validate_stream_input_gate(gate: dict[str, Any]) -> None:
        version = gate.get("continuation_input_binding_version")
        if version != STREAM_INPUT_BINDING_VERSION:
            raise ValueError("the reviewed continuation input version changed")
        if any(field not in gate for field in STREAM_INPUT_GATE_FIELDS):
            raise ValueError("the reviewed continuation input binding is incomplete")

    def _validate_stream_input_binding(
        self,
        gate: dict[str, Any],
        binding: dict[str, Any] | None,
        *,
        request_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        version = gate.get("continuation_input_binding_version")
        if version is None:
            return None
        self._validate_stream_input_gate(gate)
        if binding is None:
            raise ValueError("the reviewed continuation input is not bound")
        expected_dir = Path(str(gate["continuation_artifact"])).resolve()
        access_run_dir = binding["access_run_dir"]
        if access_run_dir.resolve() != expected_dir:
            raise ValueError("the reviewed continuation input directory changed")
        if (
            binding["run_id"] != gate["authorized_new_run_id"]
            or binding["campaign_id"] != gate["authorized_campaign_id"]
            or (
                request_run_id is not None
                and request_run_id != gate["authorized_new_run_id"]
            )
        ):
            raise ValueError("the reviewed continuation run identity changed")
        for field, gate_field in (
            ("eligibility_prompt_file", "eligibility_prompt_sha256"),
            ("eligibility_schema_file", "eligibility_schema_sha256"),
            ("eligibility_policy_file", "eligibility_policy_sha256"),
        ):
            path = binding[field]
            if not path.is_file() or sha256_file(path) != gate[gate_field]:
                raise ValueError("the reviewed continuation eligibility inputs changed")
        manifest_path = expected_dir / "run-manifest.json"
        receipt_path = expected_dir / "run-receipt.json"
        if (
            not manifest_path.is_file()
            or not receipt_path.is_file()
            or sha256_file(manifest_path) != gate["continuation_run_manifest_sha256"]
            or sha256_file(receipt_path) != gate["continuation_run_receipt_sha256"]
        ):
            raise ValueError("the reviewed continuation input receipts changed")
        manifest = _read(manifest_path)
        receipt = _read(receipt_path)
        family_count = gate["continuation_family_count"]
        selection = manifest.get("selection")
        if (
            isinstance(family_count, bool)
            or not isinstance(family_count, int)
            or family_count < 1
            or manifest.get("schema") != "article-access-manifest-v1"
            or manifest.get("run_id") != gate["continuation_access_run_id"]
            or manifest.get("target_total") != family_count
            or not isinstance(selection, list)
            or len(selection) != family_count
            or manifest.get("frozen_manifest_sha256")
            != gate["continuation_frozen_manifest_sha256"]
            or manifest.get("remaining_order_sha256")
            != gate["continuation_order_sha256"]
            or receipt.get("schema") != "article-access-run-receipt-v1"
            or receipt.get("state") != "completed"
            or receipt.get("run_id") != manifest.get("run_id")
            or receipt.get("run_manifest_sha256") != sha256_file(manifest_path)
            or receipt.get("frozen_manifest_sha256")
            not in (None, gate["continuation_frozen_manifest_sha256"])
            or receipt.get("remaining_order_sha256")
            != gate["continuation_order_sha256"]
            or (receipt.get("counts") or {}).get("target") != family_count
        ):
            raise ValueError("the reviewed continuation input identity changed")
        return binding

    def bind_stream_input(
        self,
        access_run_dir: Path,
        *,
        phase: str,
        run_id: str,
        campaign_id: str,
        eligibility_prompt_file: Path,
        eligibility_schema_file: Path,
        eligibility_policy_file: Path,
    ) -> None:
        """Bind a reviewed live continuation input before provider use."""
        gate = _validate_gate(self.execution_gate_file, phase)
        binding = {
            "access_run_dir": access_run_dir.resolve(),
            "run_id": run_id,
            "campaign_id": campaign_id,
            "eligibility_prompt_file": eligibility_prompt_file.resolve(),
            "eligibility_schema_file": eligibility_schema_file.resolve(),
            "eligibility_policy_file": eligibility_policy_file.resolve(),
        }
        self._stream_input_binding = self._validate_stream_input_binding(gate, binding)

    def stream_input_binding_required(self) -> bool:
        """Report whether this gate uses the versioned input contract."""
        return (
            _read(self.execution_gate_file).get("continuation_input_binding_version")
            is not None
        )

    def _commit_ledger(self, ledger: dict[str, Any]) -> None:
        """Make one mutation durable.

        The cost of a commit is the size of the change, not the size of the
        ledger: the rows the mutation touched are proved again, one line is
        appended to the journal, and the flush that makes it durable is
        shared with every other call that appended before it. The snapshot and
        the status file are written by the compactor, off this path.
        """
        changed = LedgerStore.changed_request_keys(ledger)
        self._validate_ledger_delta(ledger, changed)
        if not self._store.commit(ledger, now=_now()):
            return
        # This process's own mutation owes the same proof a peer's record
        # owes. A read clears the tracked keys, so they are collected here,
        # where the mutation is known, and not at the next read.
        if self._immutable_events_pending is not None:
            self._immutable_events_pending.update(changed)
        if self.deferred_snapshot:
            # A concurrent run defers the snapshot to the compactor. The
            # journal is the authority and every reader of this repository
            # reads it; the compacted file follows within
            # COMPACTION_INTERVAL_SECONDS.
            self._start_compactor()
            return
        # One operation at a time, so the snapshot costs what it always cost
        # and stays exact for every reader of the plain file.
        self._store.flush()
        with ledger_store.held_compaction_lock(self.ledger_file, wait=True):
            ledger_store.write_snapshot(
                self.ledger_file,
                ledger,
                self._store.sequence,
                journal_offset=self._store.offset,
            )
            self._compacted_seq = self._store.sequence
        self._publish_status(ledger)

    def doctor(self) -> dict[str, Any]:
        gate = _read(self.execution_gate_file)
        return {
            "schema": "shared-gemini-broker-doctor-v1",
            "credential_source": _credential_status(self.credential_file),
            "live_generation_enabled": gate.get("live_generation_enabled") is True,
            "independent_review_verdict": gate.get("independent_review_verdict"),
            "model": self.config["model"],
            "ledger": self.status(),
            "live_call_made": False,
        }

    def status(self) -> dict[str, Any]:
        with self._ledger_lock():
            ledger = self._validated_ledger()
            return self._status_payload(ledger)

    def effective_receipt_path(self, request_key: str) -> Path:
        """Return the validated path for the current final receipt."""
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid-call request key is invalid")
        with self._ledger_lock():
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if request is None:
                raise ValueError("the paid-call request does not exist")
            path = self.receipts_dir / (
                f"{self._request_event_stem(request_key, request)}.json"
            )
            if not path.is_file():
                raise ValueError("the paid-call request has no final receipt")
            return path

    def effective_receipt(self, request_key: str) -> dict[str, Any]:
        """Read a final receipt through validated reconciliation custody."""
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid-call request key is invalid")
        with self._ledger_lock():
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if request is None:
                raise ValueError("the paid-call request does not exist")
            event_stem = self._request_event_stem(request_key, request)
            final_path = self.receipts_dir / f"{event_stem}.json"
            final = _read(final_path)
            rejection_sha256 = request.get("http_rejection_settlement_sha256")
            if rejection_sha256 is not None:
                # A settled rejection has no response. The view carries the
                # settlement so a caller can never mistake it for a reusable
                # completed receipt; the ledger row is the zero-cost record.
                return {
                    **final,
                    "state": "rejected_settled",
                    "actual_cost_usd": "0",
                    "usage": dict(_ZERO_USAGE),
                    "http_rejection_settlement_sha256": rejection_sha256,
                }
            reconciliation_sha256 = request.get("usage_reconciliation_sha256")
            if reconciliation_sha256 is None:
                return final

            reconciliation_path = (
                self.receipts_dir / f"{request_key}.usage-reconciliation.json"
            )
            received_path = self.receipts_dir / f"{event_stem}.received.json"
            reconciliation = self._read_usage_reconciliation(reconciliation_path)
            received = _read(received_path)
            if (
                request.get("state") != "completed"
                or sha256_file(reconciliation_path) != reconciliation_sha256
                or reconciliation["received_receipt_sha256"]
                != sha256_file(received_path)
                or reconciliation["ambiguous_receipt_sha256"] != sha256_file(final_path)
            ):
                raise ValueError("the reconciled paid-call receipt changed")
            return {
                **final,
                "state": "completed",
                "response": received["response"],
                "usage": request["usage"],
                "actual_cost_usd": request["actual_cost_usd"],
                "usage_reconciliation_sha256": reconciliation_sha256,
                "received_receipt_sha256": reconciliation["received_receipt_sha256"],
                "ambiguous_receipt_sha256": reconciliation["ambiguous_receipt_sha256"],
            }

    def record_accepted(
        self,
        *,
        family_id: str,
        item_id: str,
        invocation_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not family_id or not item_id:
            raise ValueError("accepted item identity is missing")
        with self._ledger_lock():
            ledger = self._validated_ledger()
            old = ledger["accepted_families"].get(family_id)
            if old and old != item_id:
                gate_phase = _read(self.execution_gate_file).get("allowed_phase")
                if gate_phase not in CONSTRUCTION_PHASES:
                    raise ValueError("the accepted-item supersession phase is invalid")
                gate = _validate_gate(self.execution_gate_file, gate_phase)
                binding = self._stream_input_binding
                if (
                    gate.get("accepted_item_supersession_enabled") is not True
                    or not invocation_run_id
                    or gate.get("authorized_new_run_id") != invocation_run_id
                    or binding is None
                    or binding.get("run_id") != invocation_run_id
                ):
                    raise ValueError("a paper family already has an accepted item")
            duplicate_family = next(
                (
                    other_family
                    for other_family, accepted_item in ledger[
                        "accepted_families"
                    ].items()
                    if accepted_item == item_id and other_family != family_id
                ),
                None,
            )
            if duplicate_family:
                raise ValueError(
                    "an accepted item is already assigned to another paper family"
                )
            if not old and len(ledger["accepted_families"]) >= int(
                self.policy["accepted_question_target"]
            ):
                raise ValueError("the accepted-question target is complete")
            if not old:
                atomic_json(
                    self.receipts_dir
                    / f"accepted-{sha256_bytes(family_id.encode())}.json",
                    {
                        "schema": ACCEPTED_ITEM_V1_SCHEMA,
                        "family_id": family_id,
                        "item_id": item_id,
                        "recorded_at_utc": _now(),
                    },
                    immutable=True,
                )
            elif old != item_id:
                old_path = self.receipts_dir / (
                    f"accepted-{sha256_bytes(family_id.encode())}.json"
                )
                for path in self.receipts_dir.glob(
                    f"accepted-{sha256_bytes(family_id.encode())}-*.json"
                ):
                    event = _read(path)
                    if event.get("item_id") == old:
                        old_path = path
                        break
                if not old_path.is_file():
                    raise ValueError("the accepted-item predecessor is absent")
                atomic_json(
                    self.receipts_dir
                    / (
                        f"accepted-{sha256_bytes(family_id.encode())}-"
                        f"{sha256_bytes(item_id.encode())}.json"
                    ),
                    {
                        "schema": ACCEPTED_ITEM_V2_SCHEMA,
                        "family_id": family_id,
                        "item_id": item_id,
                        "predecessor_item_id": old,
                        "predecessor_event_sha256": sha256_file(old_path),
                        "invocation_run_id": invocation_run_id,
                        "gate_sha256": sha256_file(self.execution_gate_file),
                        "recorded_at_utc": _now(),
                    },
                    immutable=True,
                )
            ledger["accepted_families"][family_id] = item_id
            ledger["accepted_question_count"] = len(ledger["accepted_families"])
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)
        return self.status()

    def reconcile_omitted_thought_usage(self, request_key: str) -> dict[str, Any]:
        """Settle one saved response whose token total proves an omitted zero.

        Two shapes qualify, both proved by the recorded total: an omitted
        ``thoughtsTokenCount`` and an omitted ``candidatesTokenCount`` (see
        :func:`_omitted_zero_usage_field`). The settlement reads the immutable
        received response, computes the cost from the normalized usage, writes
        one immutable reconciliation receipt, moves the exact reservation from
        the ambiguous funds to the spend, and lifts the halt of that phase when
        no ambiguous request and no in-flight request remain. It reads no
        credential and makes no provider call.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the reconciled request key is invalid")
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError("the reconciled request does not exist")
                reconciliation_path = (
                    self.receipts_dir / f"{request_key}.usage-reconciliation.json"
                )
                if request.get("usage_reconciliation_sha256") is not None:
                    event = self._read_usage_reconciliation(reconciliation_path)
                    # The request is settled. Leave the ledger in the state the
                    # halt rule implies: a halt whose blocking ambiguous
                    # requests are all settled does not stand.
                    before = (
                        ledger["halted"],
                        ledger.get("evaluation_halted"),
                    )
                    self._lift_settled_halts(ledger)
                    if before != (ledger["halted"], ledger.get("evaluation_halted")):
                        ledger["updated_at_utc"] = _now()
                        self._validate_ledger(ledger)
                        self._validate_immutable_events(ledger)
                        self._commit_ledger(ledger)
                    return {
                        "schema": "shared-paid-call-usage-reconciliation-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "actual_cost_usd": event["actual_cost_usd"],
                        "reconciliation_receipt": str(reconciliation_path),
                        "reconciliation_receipt_sha256": sha256_file(
                            reconciliation_path
                        ),
                    }
                if request.get("state") != "ambiguous_charge":
                    raise ValueError("the request does not have an ambiguous charge")
                # The halt of the request's own phase must still stand: the
                # ledger halt for a construction request, the evaluation halt
                # for an evaluation request.
                if (
                    self._phase_halted(ledger, request["phase"])
                    != AMBIGUOUS_HALT_REASON
                ):
                    raise ValueError("the ambiguous-charge halt state changed")

                event_stem = self._request_event_stem(request_key, request)
                final_path = self.receipts_dir / f"{event_stem}.json"
                received_path = self.receipts_dir / f"{event_stem}.received.json"
                final = _read(final_path)
                received = _read(received_path)
                if (
                    final.get("state") != "ambiguous_charge"
                    or final.get("error") not in RECONCILABLE_USAGE_ERRORS
                    or received.get("state") != "response_received"
                    or final.get("response") != received.get("response")
                ):
                    raise ValueError(
                        "the ambiguous response is not an omitted-zero usage case"
                    )
                raw_usage = received["response"].get("usageMetadata")
                omitted = (
                    _omitted_zero_usage_field(raw_usage)
                    if isinstance(raw_usage, dict)
                    else None
                )
                if omitted is None:
                    raise ValueError(
                        "the saved usage does not omit exactly one zero token value"
                    )
                usage = _normalized_usage(received["response"])
                request_config = self.config_for_stage(request["stage"])
                if request.get("model") != request_config["model"]:
                    raise ValueError("the reconciled request model changed")
                actual = _cost(
                    request_config,
                    usage["promptTokenCount"],
                    usage["candidatesTokenCount"] + usage["thoughtsTokenCount"],
                )
                reserved = _money(
                    request.get("reserved_usd"), "reconciled reservation", positive=True
                )
                if actual > reserved:
                    raise ValueError("the reconciled cost exceeds the reservation")

                # An evaluation request is reviewed by its own evaluation gate;
                # the construction gate never allows that phase.
                if request["phase"] == EVALUATION_PHASE:
                    self._require_evaluation()
                    gate_file = self.evaluation_gate_file
                    gate = _validate_evaluation_gate(gate_file)  # type: ignore[arg-type]
                    self._validate_evaluation_gate_hashes(gate)
                else:
                    gate_file = self.execution_gate_file
                    gate = _validate_gate(gate_file, request["phase"])
                review_path = Path(gate["review_record"]).resolve()
                if not review_path.is_file() or gate.get(
                    "review_record_sha256"
                ) != sha256_file(review_path):
                    raise ValueError("the usage reconciliation review is invalid")
                event = {
                    "schema": "shared-paid-call-usage-reconciliation-v1",
                    "request_key": request_key,
                    "received_receipt_sha256": sha256_file(received_path),
                    "ambiguous_receipt_sha256": sha256_file(final_path),
                    "config_transition_sha256": request.get("config_transition_sha256"),
                    "price_config_sha256": request.get(
                        "price_config_sha256", ledger["price_config_sha256"]
                    ),
                    "normalized_usage": usage,
                    "actual_cost_usd": str(actual),
                    "ledger_sha256_before": sha256_file(self.ledger_file),
                    "omitted_zero_usage_field": omitted,
                    "phase": request["phase"],
                    "gate_sha256": sha256_file(gate_file),
                    "integrated_code_commit": gate["integrated_code_commit"],
                    "review_record": str(review_path),
                    "review_record_sha256": gate["review_record_sha256"],
                    "reconciled_at_utc": _now(),
                }
                if reconciliation_path.is_file():
                    existing = self._read_usage_reconciliation(reconciliation_path)
                    comparison = dict(event)
                    comparison["reconciled_at_utc"] = existing["reconciled_at_utc"]
                    if existing != comparison:
                        raise ValueError("the usage reconciliation event changed")
                    event = existing
                else:
                    atomic_json(reconciliation_path, event, immutable=True)
                reconciliation_sha256 = sha256_file(reconciliation_path)

                ledger["ambiguous_reserved_usd"] = str(
                    _money(ledger["ambiguous_reserved_usd"], "ambiguous") - reserved
                )
                ledger["spent_usd"] = str(_money(ledger["spent_usd"], "spent") + actual)
                stage = ledger["stages"][request["stage"]]
                paper = ledger["papers"][request["family_id"]]
                for row in (stage, paper):
                    row["ambiguous_usd"] = str(
                        _money(row["ambiguous_usd"], "ambiguous") - reserved
                    )
                    row["spent_usd"] = str(_money(row["spent_usd"], "spent") + actual)
                    row["input_tokens"] += usage["promptTokenCount"]
                    row["output_tokens"] += usage["candidatesTokenCount"]
                    row["thinking_tokens"] += usage["thoughtsTokenCount"]
                if request["phase"] == "live_test":
                    live = ledger["live_test_papers"][request["family_id"]]
                    live["ambiguous_usd"] = str(
                        _money(live["ambiguous_usd"], "live ambiguous") - reserved
                    )
                    live["spent_usd"] = str(
                        _money(live["spent_usd"], "live spent") + actual
                    )
                request.update(
                    {
                        "state": "completed",
                        "actual_cost_usd": str(actual),
                        "usage": usage,
                        "usage_reconciliation_sha256": reconciliation_sha256,
                        "reconciled_at_utc": event["reconciled_at_utc"],
                    }
                )
                self._lift_settled_halts(ledger)
                ledger["updated_at_utc"] = _now()
                self._validate_ledger(ledger)
                self._validate_immutable_events(ledger)
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-usage-reconciliation-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "actual_cost_usd": str(actual),
                    "reconciliation_receipt": str(reconciliation_path),
                    "reconciliation_receipt_sha256": reconciliation_sha256,
                }
        finally:
            operation.close()

    def authorize_ambiguous_continuation(
        self,
        *,
        request_key: str,
        expected_ledger_sha256: str,
        review_file: Path,
        evidence_file: Path,
        authorized_run_id: str,
        operator_id: str,
    ) -> dict[str, Any]:
        """Authorize unrelated work after one preserved ambiguous charge.

        This operation never settles or retries the ambiguous request. It only
        records the reviewed skip and releases the global stop when every
        ambiguous request from the authorized run has the same custody.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the ambiguous continuation request key is invalid")
        if not authorized_run_id.strip() or not operator_id.strip():
            raise ValueError("the ambiguous continuation operator identity is missing")
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError(
                        "the ambiguous continuation request does not exist"
                    )
                continuation_path = (
                    self.receipts_dir / f"ambiguous-continuation-{request_key}.json"
                )
                if continuation_path.is_file():
                    event = self._read_ambiguous_continuation(continuation_path)
                    return {
                        "schema": "shared-paid-call-ambiguous-continuation-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "continuation_receipt": str(continuation_path),
                        "continuation_receipt_sha256": sha256_file(continuation_path),
                        "affected_family_id": event["affected_family_id"],
                    }
                if sha256_file(self.ledger_file) != expected_ledger_sha256:
                    raise ValueError("the ambiguous continuation ledger changed")
                if request.get("state") != "ambiguous_charge":
                    raise ValueError("the request does not have an ambiguous charge")
                # The halt this release lifts is the one that covers the
                # request's own phase. An ambiguous construction charge halts
                # the whole ledger; an ambiguous ``benchmark_evaluation``
                # charge halts the evaluation phase alone, through
                # ``evaluation_halted``, so the construction pipeline keeps
                # running.
                halt = self._phase_halted(ledger, request["phase"])
                if halt != AMBIGUOUS_HALT_REASON:
                    raise ValueError("the ambiguous-charge halt state changed")
                if request.get("run_id") != authorized_run_id:
                    raise ValueError(
                        "the request is outside the authorized continuation run"
                    )
                # An evaluation request is reviewed by its own evaluation gate;
                # the construction gate never allows that phase. The two gates
                # name the authorized run in their own field.
                if request["phase"] == EVALUATION_PHASE:
                    self._require_evaluation()
                    gate_file = self.evaluation_gate_file
                    gate = _validate_evaluation_gate(gate_file)  # type: ignore[arg-type]
                    self._validate_evaluation_gate_hashes(gate)
                    gate_run_id = gate.get("authorized_run_id")
                else:
                    gate_file = self.execution_gate_file
                    gate = _validate_gate(gate_file, request["phase"])
                    gate_run_id = gate.get("authorized_new_run_id")
                gate_sha256 = sha256_file(gate_file)  # type: ignore[arg-type]
                if (
                    request.get("gate_sha256") != gate_sha256
                    or gate_run_id != authorized_run_id
                ):
                    raise ValueError("the ambiguous continuation gate changed")
                final_path = self.receipts_dir / f"{request_key}.json"
                received_path = self.receipts_dir / f"{request_key}.received.json"
                trace_path = self.receipts_dir / f"{request_key}.request-trace.json"
                final = _read(final_path)
                reserved = _money(
                    request.get("reserved_usd"), "ambiguous reservation", positive=True
                )
                # One bounded case for every 5xx answer: the provider reported a
                # server error, the charge is unknown, and no response was
                # received. The evidence must name the receipt's own status.
                http_500_case = (
                    final.get("state") == "ambiguous_charge"
                    and final.get("error_class") == "known_http_response_unknown_charge"
                    and _is_server_error_status(final.get("http_status"))
                    and final.get("live_call_made") is True
                    and "response" not in final
                    and not received_path.exists()
                    and final.get("reserved_usd") == request.get("reserved_usd")
                    and final.get("actual_cost_usd") is None
                )
                received_max_tokens_case = (
                    received_path.is_file()
                    and trace_path.is_file()
                    and _is_received_max_tokens_ambiguous_case(
                        final,
                        _read(received_path),
                        _read(trace_path),
                        request,
                    )
                )
                # One bounded case for a provider timeout: the client stopped
                # waiting, nothing came back, and the charge is unknown for the
                # same reason a 5xx answer is. The evidence must name the
                # timeout the call actually ran under.
                provider_timeout_case = _is_provider_timeout_ambiguous_case(
                    final,
                    request,
                    received_receipt_present=received_path.exists(),
                )
                timeout_seconds = (
                    _receipt_timeout_seconds(final) if provider_timeout_case else None
                )
                if (
                    not http_500_case
                    and not received_max_tokens_case
                    and not provider_timeout_case
                ):
                    raise ValueError("the request is not a supported ambiguous case")
                if not review_file.is_file() or not evidence_file.is_file():
                    raise ValueError("the ambiguous continuation evidence is absent")
                evidence = _read(evidence_file)
                if http_500_case:
                    expected_evidence = {
                        "schema": AMBIGUOUS_CONTINUATION_EVIDENCE_SCHEMA,
                        "request_key": request_key,
                        "error_class": "known_http_response_unknown_charge",
                        "http_status": final["http_status"],
                        "live_call_made": True,
                        "received_receipt_absent": True,
                        "actual_cost_known": False,
                        "replay_prohibited": True,
                        "affected_family_id": request["family_id"],
                        "authorized_run_id": authorized_run_id,
                    }
                elif provider_timeout_case:
                    expected_evidence = {
                        "schema": PROVIDER_TIMEOUT_CONTINUATION_EVIDENCE_SCHEMA,
                        "request_key": request_key,
                        "error_class": PROVIDER_TIMEOUT_ERROR_CLASS,
                        "error": PROVIDER_TIMEOUT_ERROR,
                        "timeout_seconds": timeout_seconds,
                        "live_call_made": True,
                        "received_receipt_absent": True,
                        "actual_cost_known": False,
                        "replay_prohibited": True,
                        "affected_family_id": request["family_id"],
                        "authorized_run_id": authorized_run_id,
                    }
                else:
                    expected_evidence = {
                        "schema": RECEIVED_MAX_TOKENS_CONTINUATION_EVIDENCE_SCHEMA,
                        "request_key": request_key,
                        "error_class": "received_max_tokens_usage_unknown",
                        "finish_reason": "MAX_TOKENS",
                        "live_call_made": True,
                        "received_receipt_present": True,
                        "actual_cost_known": False,
                        "replay_prohibited": True,
                        "affected_family_id": request["family_id"],
                        "authorized_run_id": authorized_run_id,
                    }
                if evidence != expected_evidence:
                    raise ValueError("the ambiguous continuation evidence is not exact")
                continuation_events = self._ambiguous_continuation_events(ledger)
                if any(
                    other.get("state") == "ambiguous_charge"
                    and other_key != request_key
                    and other_key not in continuation_events
                    for other_key, other in ledger["requests"].items()
                ):
                    raise ValueError(
                        "every outstanding ambiguous request needs a continuation event"
                    )
                if http_500_case:
                    schema = AMBIGUOUS_CONTINUATION_SCHEMA
                    error_class = "known_http_response_unknown_charge"
                    skip_reason_code = "operational_ambiguous_charge_http_500"
                elif provider_timeout_case:
                    schema = PROVIDER_TIMEOUT_CONTINUATION_SCHEMA
                    error_class = PROVIDER_TIMEOUT_ERROR_CLASS
                    skip_reason_code = PROVIDER_TIMEOUT_SKIP_REASON
                else:
                    schema = RECEIVED_MAX_TOKENS_CONTINUATION_SCHEMA
                    error_class = "received_max_tokens_usage_unknown"
                    skip_reason_code = (
                        "operational_ambiguous_charge_received_max_tokens"
                    )
                event = {
                    "schema": schema,
                    "request_key": request_key,
                    "ambiguous_receipt_sha256": sha256_file(final_path),
                    "request_identity": {
                        key: request[key]
                        for key in (
                            "run_id",
                            "stage",
                            "paper_id",
                            "family_id",
                            "source_version_id",
                            "request_sha256",
                            "reserved_usd",
                        )
                    },
                    "error_class": error_class,
                    "live_call_made": True,
                    "reserved_usd": str(reserved),
                    "reservation_policy": AMBIGUOUS_CONTINUATION_RESERVATION_POLICY,
                    "scope": "unrelated_families_only",
                    "affected_family_id": request["family_id"],
                    "skip_reason_code": skip_reason_code,
                    "authorized_run_id": authorized_run_id,
                    "evidence_file": str(evidence_file.resolve()),
                    "evidence_file_sha256": sha256_file(evidence_file),
                    "review_file": str(review_file.resolve()),
                    "review_file_sha256": sha256_file(review_file),
                    "ledger_sha256_before": expected_ledger_sha256,
                    "gate_sha256": gate_sha256,
                    "integrated_code_commit": gate["integrated_code_commit"],
                    "authorized_at_utc": _now(),
                    "operator_id": operator_id,
                }
                if http_500_case:
                    event.update(
                        {
                            "http_status": final["http_status"],
                            "received_receipt_absent": True,
                        }
                    )
                elif provider_timeout_case:
                    event.update(
                        {
                            "error": PROVIDER_TIMEOUT_ERROR,
                            "timeout_seconds": timeout_seconds,
                            "received_receipt_absent": True,
                        }
                    )
                else:
                    event.update(
                        {
                            "received_receipt_sha256": sha256_file(received_path),
                            "request_trace_sha256": sha256_file(trace_path),
                            "finish_reason": "MAX_TOKENS",
                            "received_receipt_present": True,
                        }
                    )
                atomic_json(continuation_path, event, immutable=True)
                # Lift exactly the halt the ambiguous charge set, and only for
                # the request's own phase. A released evaluation ambiguity
                # never lifts a construction halt, and the reverse.
                if request["phase"] == EVALUATION_PHASE:
                    ledger["evaluation_halted"] = False
                    ledger["evaluation_halt_reason"] = None
                else:
                    ledger["halted"] = False
                    ledger["halt_reason"] = None
                ledger["updated_at_utc"] = _now()
                self._commit_ledger(ledger)
                continuation_sha256 = sha256_file(continuation_path)
                return {
                    "schema": "shared-paid-call-ambiguous-continuation-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "continuation_receipt": str(continuation_path),
                    "continuation_receipt_sha256": continuation_sha256,
                    "affected_family_id": request["family_id"],
                    "reserved_usd_retained": str(reserved),
                    "replay_prohibited": True,
                }
        finally:
            operation.close()

    def authorize_orphaned_request_continuation(
        self,
        *,
        request_key: str,
        expected_ledger_sha256: str,
        review_file: Path,
        evidence_file: Path,
        authorized_run_id: str,
        operator_id: str,
    ) -> dict[str, Any]:
        """Release only concurrency for one dead-owner submitted request."""
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the orphaned continuation request key is invalid")
        if not authorized_run_id.strip() or not operator_id.strip():
            raise ValueError("the orphaned continuation operator identity is missing")
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError("the orphaned continuation request does not exist")
                path = self.receipts_dir / f"orphaned-continuation-{request_key}.json"
                if path.is_file():
                    event = self._read_orphaned_continuation(path)
                    return {
                        "schema": "shared-paid-call-orphaned-continuation-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "continuation_receipt": str(path),
                        "continuation_receipt_sha256": sha256_file(path),
                        "affected_family_id": event["affected_family_id"],
                        "reserved_usd_retained": event["reserved_usd"],
                        "replay_prohibited": True,
                    }
                if sha256_file(self.ledger_file) != expected_ledger_sha256:
                    raise ValueError("the orphaned continuation ledger changed")
                if request.get("state") != "submitted":
                    raise ValueError("the request is not a submitted liability")
                gate = _validate_gate(self.execution_gate_file, request["phase"])
                gate_sha256 = sha256_file(self.execution_gate_file)
                if gate.get("authorized_new_run_id") != authorized_run_id:
                    raise ValueError("the orphaned continuation run is not authorized")
                event_stem = self._request_event_stem(request_key, request)
                submitted_path = self.receipts_dir / f"{event_stem}.submitted.json"
                trace_path = self.receipts_dir / f"{request_key}.request-trace.json"
                final_path = self.receipts_dir / f"{event_stem}.json"
                received_path = self.receipts_dir / f"{event_stem}.received.json"
                if (
                    not submitted_path.is_file()
                    or not trace_path.is_file()
                    or final_path.exists()
                    or received_path.exists()
                    or not review_file.is_file()
                    or not evidence_file.is_file()
                ):
                    raise ValueError(
                        "the orphaned continuation custody evidence is absent"
                    )
                evidence = _read(evidence_file)
                expected_evidence = {
                    "schema": ORPHANED_CONTINUATION_EVIDENCE_SCHEMA,
                    "request_key": request_key,
                    "submitted_receipt_present": True,
                    "request_trace_present": True,
                    "final_receipt_absent": True,
                    "received_receipt_absent": True,
                    "provider_usage_known": False,
                    "owner_process_confirmed_dead": True,
                    "replay_prohibited": True,
                    "affected_family_id": request["family_id"],
                    "authorized_run_id": authorized_run_id,
                }
                if evidence != expected_evidence:
                    raise ValueError("the orphaned continuation evidence is not exact")
                reserved = _money(
                    request["reserved_usd"], "orphan reservation", positive=True
                )
                event = {
                    "schema": ORPHANED_CONTINUATION_SCHEMA,
                    "request_key": request_key,
                    "submitted_receipt_sha256": sha256_file(submitted_path),
                    "request_trace_sha256": sha256_file(trace_path),
                    "request_identity": {
                        key: request[key]
                        for key in (
                            "run_id",
                            "stage",
                            "paper_id",
                            "family_id",
                            "source_version_id",
                            "request_sha256",
                            "reserved_usd",
                            "submitted_at_utc",
                        )
                    },
                    "reserved_usd": str(reserved),
                    "reservation_policy": ORPHANED_CONTINUATION_RESERVATION_POLICY,
                    "scope": "unrelated_families_only",
                    "affected_family_id": request["family_id"],
                    "skip_reason_code": "operational_orphaned_request_no_replay",
                    "authorized_run_id": authorized_run_id,
                    "evidence_file": str(evidence_file.resolve()),
                    "evidence_file_sha256": sha256_file(evidence_file),
                    "review_file": str(review_file.resolve()),
                    "review_file_sha256": sha256_file(review_file),
                    "ledger_sha256_before": expected_ledger_sha256,
                    "gate_sha256": gate_sha256,
                    "integrated_code_commit": gate["integrated_code_commit"],
                    "authorized_at_utc": _now(),
                    "operator_id": operator_id,
                }
                atomic_json(path, event, immutable=True)
                request["state"] = "orphaned_no_replay"
                request["orphaned_continuation_sha256"] = sha256_file(path)
                ledger["inflight"] = int(ledger["inflight"]) - 1
                ledger["updated_at_utc"] = _now()
                self._validate_immutable_events(ledger)
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-orphaned-continuation-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "continuation_receipt": str(path),
                    "continuation_receipt_sha256": sha256_file(path),
                    "affected_family_id": request["family_id"],
                    "reserved_usd_retained": str(reserved),
                    "replay_prohibited": True,
                }
        finally:
            operation.close()

    def settle_http_rejection(
        self,
        *,
        request_key: str,
        expected_ledger_sha256: str,
        review_file: Path,
        evidence_file: Path,
        authorized_run_id: str,
        operator_id: str,
    ) -> dict[str, Any]:
        """Settle one ambiguous charge that a provider rejection created.

        The provider answered the generation request with a rejection status
        before any generation ran, so it billed nothing. The evidence is the
        error body the receipt recorded, or a reproduction of the exact same
        request (same request sha256) that received the same rejection. The
        reservation leaves the ambiguous funds, the request settles at zero
        cost, and the halt lifts when every other ambiguous request has its
        reviewed continuation. Nothing is replayed: the request key stays a
        settled terminal record, and a corrected request has a new key.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the http rejection request key is invalid")
        if not authorized_run_id.strip() or not operator_id.strip():
            raise ValueError("the http rejection operator identity is missing")
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError("the http rejection request does not exist")
                settlement_path = (
                    self.receipts_dir / f"{request_key}.http-rejection-settlement.json"
                )
                applied = request.get("http_rejection_settlement_sha256")
                if applied is not None:
                    if (
                        not settlement_path.is_file()
                        or sha256_file(settlement_path) != applied
                    ):
                        raise ValueError("the http rejection settlement changed")
                    return {
                        "schema": "shared-paid-call-http-rejection-settlement-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "settlement_receipt": str(settlement_path),
                        "settlement_receipt_sha256": applied,
                        "released_usd": request.get("reserved_usd"),
                    }
                if sha256_file(self.ledger_file) != expected_ledger_sha256:
                    raise ValueError("the http rejection ledger changed")
                if request.get("state") != "ambiguous_charge":
                    raise ValueError("the request does not have an ambiguous charge")
                if (
                    ledger.get("halted") is not True
                    or ledger.get("halt_reason") != "ambiguous_generation_charge"
                ):
                    raise ValueError("the ambiguous-charge halt state changed")
                if request.get("run_id") != authorized_run_id:
                    raise ValueError("the request is outside the authorized run")
                gate = _validate_gate(self.execution_gate_file, request["phase"])
                gate_sha256 = sha256_file(self.execution_gate_file)
                if (
                    request.get("gate_sha256") != gate_sha256
                    or gate.get("authorized_new_run_id") != authorized_run_id
                ):
                    raise ValueError("the http rejection gate changed")
                event_stem = self._request_event_stem(request_key, request)
                final_path = self.receipts_dir / f"{event_stem}.json"
                received_path = self.receipts_dir / f"{event_stem}.received.json"
                final = _read(final_path)
                if settlement_path.exists():
                    raise ValueError("an unapplied http rejection settlement exists")
                if (
                    final.get("state") != "ambiguous_charge"
                    or final.get("error_class") != "known_http_response_unknown_charge"
                    or final.get("http_status") not in HTTP_REJECTION_STATUSES
                    or final.get("live_call_made") is not True
                    or "response" in final
                    or received_path.exists()
                    or final.get("reserved_usd") != request.get("reserved_usd")
                    or final.get("actual_cost_usd") is not None
                ):
                    raise ValueError("the request is not a provider rejection case")
                if not review_file.is_file() or not evidence_file.is_file():
                    raise ValueError("the http rejection evidence is absent")
                evidence = _read(evidence_file)
                recorded_status = final.get("provider_error_status")
                reproduction = evidence.get("reproduction")
                if recorded_status is not None:
                    source = "receipt"
                    provider_status = recorded_status
                    if reproduction is not None:
                        raise ValueError(
                            "the receipt records the rejection; no reproduction is read"
                        )
                else:
                    source = "reproduction"
                    if not isinstance(reproduction, dict):
                        raise ValueError("the http rejection reproduction is absent")
                    record_path = Path(str(reproduction.get("record_file") or ""))
                    if not record_path.is_file() or reproduction.get(
                        "record_file_sha256"
                    ) != sha256_file(record_path):
                        raise ValueError("the http rejection reproduction changed")
                    record = _read(record_path)
                    body_status = _provider_error_status(record.get("response_body"))
                    if (
                        record.get("request_sha256_from_trace")
                        != request.get("request_sha256")
                        or record.get("model") != request.get("model")
                        or record.get("http_status") != final.get("http_status")
                        or body_status is None
                        or reproduction.get("request_sha256")
                        != request.get("request_sha256")
                        or reproduction.get("http_status") != final.get("http_status")
                        or reproduction.get("provider_error_status") != body_status
                    ):
                        raise ValueError(
                            "the http rejection reproduction does not match the request"
                        )
                    provider_status = body_status
                if provider_status not in HTTP_REJECTION_PROVIDER_STATUSES:
                    raise ValueError("the provider status is not a rejection")
                expected_evidence = {
                    "schema": HTTP_REJECTION_EVIDENCE_SCHEMA,
                    "request_key": request_key,
                    "error_class": "known_http_response_unknown_charge",
                    "http_status": final["http_status"],
                    "provider_error_status": provider_status,
                    "error_body_source": source,
                    "reproduction": reproduction,
                    "live_call_made": True,
                    "received_receipt_absent": True,
                    "generation_started": False,
                    "actual_cost_known": True,
                    "actual_cost_usd": "0",
                    "replay_prohibited": True,
                    "affected_family_id": request["family_id"],
                    "authorized_run_id": authorized_run_id,
                }
                if evidence != expected_evidence:
                    raise ValueError("the http rejection evidence is not exact")
                continuation_events = self._ambiguous_continuation_events(ledger)
                others_unresolved = any(
                    other.get("state") == "ambiguous_charge"
                    and other_key != request_key
                    and other_key not in continuation_events
                    for other_key, other in ledger["requests"].items()
                )
                reserved = _money(
                    request.get("reserved_usd"), "rejected reservation", positive=True
                )
                event = {
                    "schema": HTTP_REJECTION_SETTLEMENT_SCHEMA,
                    "request_key": request_key,
                    "ambiguous_receipt_sha256": sha256_file(final_path),
                    "request_identity": {
                        key: request[key]
                        for key in (
                            "run_id",
                            "stage",
                            "paper_id",
                            "family_id",
                            "source_version_id",
                            "request_sha256",
                            "reserved_usd",
                            "submitted_at_utc",
                        )
                    },
                    "http_status": final["http_status"],
                    "provider_error_status": provider_status,
                    "error_body_source": source,
                    "reproduction_record": (
                        str(Path(reproduction["record_file"]).resolve())
                        if source == "reproduction"
                        else None
                    ),
                    "reproduction_record_sha256": (
                        reproduction["record_file_sha256"]
                        if source == "reproduction"
                        else None
                    ),
                    "reserved_usd": str(reserved),
                    "actual_cost_usd": "0",
                    "live_call_made": True,
                    "generation_started": False,
                    "replay_prohibited": True,
                    "evidence_file": str(evidence_file.resolve()),
                    "evidence_file_sha256": sha256_file(evidence_file),
                    "review_file": str(review_file.resolve()),
                    "review_file_sha256": sha256_file(review_file),
                    "ledger_sha256_before": expected_ledger_sha256,
                    "gate_sha256": gate_sha256,
                    "integrated_code_commit": gate["integrated_code_commit"],
                    "authorized_run_id": authorized_run_id,
                    "operator_id": operator_id,
                    "settled_at_utc": _now(),
                }
                assert set(event) == HTTP_REJECTION_SETTLEMENT_FIELDS
                atomic_json(settlement_path, event, immutable=True)
                settlement_sha256 = sha256_file(settlement_path)
                ledger["ambiguous_reserved_usd"] = str(
                    _money(ledger["ambiguous_reserved_usd"], "ambiguous") - reserved
                )
                for row in (
                    ledger["stages"][request["stage"]],
                    ledger["papers"][request["family_id"]],
                ):
                    row["ambiguous_usd"] = str(
                        _money(row["ambiguous_usd"], "ambiguous") - reserved
                    )
                if request["phase"] == "live_test":
                    live = ledger["live_test_papers"][request["family_id"]]
                    live["ambiguous_usd"] = str(
                        _money(live["ambiguous_usd"], "live ambiguous") - reserved
                    )
                request.update(
                    {
                        "state": "completed",
                        "actual_cost_usd": "0",
                        "usage": dict(_ZERO_USAGE),
                        "http_rejection_settlement_sha256": settlement_sha256,
                        "settled_at_utc": event["settled_at_utc"],
                    }
                )
                if not others_unresolved and int(ledger["inflight"]) == 0:
                    ledger["halted"] = False
                    ledger["halt_reason"] = None
                ledger["updated_at_utc"] = _now()
                self._validate_ledger(ledger)
                self._validate_immutable_events(ledger)
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-http-rejection-settlement-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "settlement_receipt": str(settlement_path),
                    "settlement_receipt_sha256": settlement_sha256,
                    "released_usd": str(reserved),
                    "halted": ledger["halted"],
                    "affected_family_id": request["family_id"],
                    "replay_prohibited": True,
                }
        finally:
            operation.close()

    def family_cost_state(self, family_id: str) -> dict[str, str]:
        """Return one paper family's committed spend against the paper cap.

        A family the cap stopped is recorded with the money it already holds, so
        the skip is legible without a second read of the ledger.
        """
        with self._ledger_lock():
            ledger = self._validated_ledger()
            row = ledger["papers"].get(family_id, {})
            used = sum(
                _money(row.get(name, 0), f"paper {name}")
                for name in ("reserved_usd", "spent_usd", "ambiguous_usd")
            )
            cap = _money(self.policy["maximum_paper_cost_usd"], "paper cap")
            return {
                "family_id": family_id,
                "reserved_usd": str(
                    _money(row.get("reserved_usd", 0), "paper reserved")
                ),
                "spent_usd": str(_money(row.get("spent_usd", 0), "paper spent")),
                "ambiguous_usd": str(
                    _money(row.get("ambiguous_usd", 0), "paper ambiguous")
                ),
                "committed_usd": str(used),
                "maximum_paper_cost_usd": str(cap),
                "remaining_usd": str(cap - used),
            }

    def operational_unresolved_families(self) -> dict[str, dict[str, str]]:
        """Return reviewed no-replay families with their exact skip reasons."""
        with self._ledger_lock():
            ledger = self._validated_ledger()
            events = [
                *self._ambiguous_continuation_events(ledger).items(),
                *self._orphaned_continuation_events(ledger).items(),
            ]
            return {
                event["affected_family_id"]: {
                    "request_key": request_key,
                    "reason_code": event["skip_reason_code"],
                }
                for request_key, event in events
            }

    def operational_unresolved_family_ids(self) -> dict[str, str]:
        return {
            family_id: value["request_key"]
            for family_id, value in self.operational_unresolved_families().items()
        }

    def settle_pretransport_reservation(
        self,
        *,
        request_key: str,
        expected_ledger_sha256: str,
        review_file: Path,
        traceback_evidence_file: Path,
    ) -> dict[str, Any]:
        """Settle one reviewed reservation that stopped before provider transport."""
        if request_key != PRETRANSPORT_SETTLEMENT_REQUEST["request_key"]:
            raise ValueError("the request is not approved for pretransport settlement")
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError("the reviewed reservation does not exist")
                settlement_path = (
                    self.receipts_dir / f"{request_key}.pretransport-settlement.json"
                )
                applied_hash = request.get("pretransport_settlement_sha256")
                if applied_hash is not None:
                    if (
                        not settlement_path.is_file()
                        or sha256_file(settlement_path) != applied_hash
                    ):
                        raise ValueError("the pretransport settlement record changed")
                    return {
                        "schema": "shared-paid-call-pretransport-settlement-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "settlement_receipt": str(settlement_path),
                        "settlement_receipt_sha256": applied_hash,
                    }
                if sha256_file(self.ledger_file) != expected_ledger_sha256:
                    raise ValueError("the reviewed reservation ledger changed")
                if any(
                    request.get(field) != value
                    for field, value in PRETRANSPORT_SETTLEMENT_REQUEST.items()
                ):
                    raise ValueError("the reviewed reservation identity changed")
                event_stem = self._request_event_stem(request_key, request)
                sidecars = {
                    "final": self.receipts_dir / f"{event_stem}.json",
                    "submitted": self.receipts_dir / f"{event_stem}.submitted.json",
                    "received": self.receipts_dir / f"{event_stem}.received.json",
                    "trace": self.receipts_dir / f"{request_key}.request-trace.json",
                }
                if settlement_path.exists() or any(
                    path.exists() for path in sidecars.values()
                ):
                    raise ValueError("a pretransport settlement sidecar already exists")
                if not review_file.is_file() or not traceback_evidence_file.is_file():
                    raise ValueError("the reviewed pretransport evidence is absent")
                _validate_gate(self.execution_gate_file, request["phase"])
                if request.get("gate_sha256") != sha256_file(self.execution_gate_file):
                    raise ValueError("the reviewed reservation gate changed")
                reserved = _money(request["reserved_usd"], "reservation", positive=True)
                event = {
                    "schema": PRETRANSPORT_SETTLEMENT_SCHEMA,
                    "request_key": request_key,
                    "ledger_sha256_before": expected_ledger_sha256,
                    "request_identity": {
                        key: request[key]
                        for key in (
                            "run_id",
                            "stage",
                            "paper_id",
                            "family_id",
                            "source_version_id",
                            "request_sha256",
                            "reserved_usd",
                            "submitted_at_utc",
                        )
                    },
                    "sidecars_absent": sorted(sidecars),
                    "traceback_evidence_file": str(traceback_evidence_file.resolve()),
                    "traceback_evidence_sha256": sha256_file(traceback_evidence_file),
                    "review_file": str(review_file.resolve()),
                    "review_file_sha256": sha256_file(review_file),
                    "gate_sha256": sha256_file(self.execution_gate_file),
                    "config_transition_sha256": request.get("config_transition_sha256"),
                    "actual_cost_usd": "0",
                    "live_call_made": False,
                    "settled_at_utc": _now(),
                }
                atomic_json(settlement_path, event, immutable=True)
                settlement_sha256 = sha256_file(settlement_path)
                final_path = sidecars["final"]
                final = {
                    **request,
                    "state": "completed",
                    "actual_cost_usd": "0",
                    "usage": {
                        "promptTokenCount": 0,
                        "candidatesTokenCount": 0,
                        "thoughtsTokenCount": 0,
                    },
                    "live_call_made": False,
                    "pretransport_settlement_sha256": settlement_sha256,
                    "completed_at_utc": _now(),
                }
                atomic_json(final_path, final, immutable=True)
                ledger["reserved_usd"] = str(
                    _money(ledger["reserved_usd"], "reserved") - reserved
                )
                ledger["inflight"] -= 1
                for row in (
                    ledger["stages"][request["stage"]],
                    ledger["papers"][request["family_id"]],
                ):
                    row["reserved_usd"] = str(
                        _money(row["reserved_usd"], "reserved") - reserved
                    )
                if request["phase"] == "live_test":
                    live = ledger["live_test_papers"][request["family_id"]]
                    live["reserved_usd"] = str(
                        _money(live["reserved_usd"], "reserved") - reserved
                    )
                request.update(
                    {
                        "state": "completed",
                        "actual_cost_usd": "0",
                        "usage": final["usage"],
                        "pretransport_settlement_sha256": settlement_sha256,
                        "completed_at_utc": final["completed_at_utc"],
                    }
                )
                ledger["updated_at_utc"] = _now()
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-pretransport-settlement-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "settlement_receipt": str(settlement_path),
                    "settlement_receipt_sha256": settlement_sha256,
                }
        finally:
            operation.close()

    def settle_phaseless_refusal(
        self,
        *,
        request_key: str,
        expected_ledger_sha256: str,
        review_file: Path,
        superseded_integrity_halt_file: Path,
    ) -> dict[str, Any]:
        """Record the phase of one reviewed refusal row that carries none.

        A `not_submitted` row made no provider call and holds no money, so
        this settlement moves no cost. It writes the one field the row lacks,
        `phase`, and only when the row's own evidence proves that phase: the
        stage prefix `evaluation_answer:`, the evaluation trial, the
        evaluation gate and the evaluation policy hash. Every other field of
        the row stays exactly as the evaluator wrote it.
        """
        if request_key != PHASELESS_REFUSAL_SETTLEMENT_REQUEST["request_key"]:
            raise ValueError("the request is not approved for phase settlement")
        if not superseded_integrity_halt_file.is_file():
            raise ValueError("the superseded integrity halt record is absent")
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError("the reviewed refusal does not exist")
                settlement_path = (
                    self.receipts_dir / f"{request_key}.phase-settlement.json"
                )
                applied_hash = request.get("phase_settlement_sha256")
                if applied_hash is not None:
                    if (
                        not settlement_path.is_file()
                        or sha256_file(settlement_path) != applied_hash
                    ):
                        raise ValueError("the phase settlement record changed")
                    return {
                        "schema": "shared-paid-call-phase-settlement-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "settlement_receipt": str(settlement_path),
                        "settlement_receipt_sha256": applied_hash,
                    }
                if sha256_file(self.ledger_file) != expected_ledger_sha256:
                    raise ValueError("the reviewed refusal ledger changed")
                if any(
                    request.get(field) != value
                    for field, value in PHASELESS_REFUSAL_SETTLEMENT_REQUEST.items()
                ):
                    raise ValueError("the reviewed refusal identity changed")
                if request.get("phase") is not None:
                    raise ValueError("the reviewed refusal already carries a phase")
                # A refusal that never reached the provider. Any of these
                # fields would mean a call, a reservation or a charge, and
                # this settlement must never touch one.
                if (
                    request.get("submitted_at_utc") is not None
                    or request.get("usage") is not None
                    or _money(request.get("reserved_usd") or "0", "reserved") != 0
                    or _money(request.get("actual_cost_usd") or "0", "cost") != 0
                ):
                    raise ValueError("the reviewed refusal is not free of money")
                if settlement_path.exists():
                    raise ValueError("a phase settlement sidecar already exists")
                if not review_file.is_file():
                    raise ValueError("the reviewed phase evidence is absent")
                evidence = {
                    "stage_prefix": f"{EVALUATION_STAGE_PREFIX}",
                    "evaluation_trial": request.get("evaluation_trial"),
                    "evaluation_gate_sha256": request.get("evaluation_gate_sha256"),
                    "evaluation_policy_sha256": request.get("evaluation_policy_sha256"),
                }
                if not str(request["stage"]).startswith(EVALUATION_STAGE_PREFIX) or any(
                    value is None for value in evidence.values()
                ):
                    raise ValueError("the reviewed refusal does not prove its phase")
                halt = _read(superseded_integrity_halt_file)
                if halt.get("reason") != PHASELESS_REFUSAL_INTEGRITY_HALT_REASON:
                    raise ValueError("the superseded integrity halt is another halt")
                event = {
                    "schema": PHASELESS_REFUSAL_SETTLEMENT_SCHEMA,
                    "request_key": request_key,
                    "ledger_sha256_before": expected_ledger_sha256,
                    "request_identity": dict(PHASELESS_REFUSAL_SETTLEMENT_REQUEST),
                    "phase_evidence": evidence,
                    "recorded_phase": EVALUATION_PHASE,
                    "review_file": str(review_file.resolve()),
                    "review_file_sha256": sha256_file(review_file),
                    "superseded_integrity_halt_file": str(
                        superseded_integrity_halt_file.resolve()
                    ),
                    "superseded_integrity_halt_sha256": sha256_file(
                        superseded_integrity_halt_file
                    ),
                    "actual_cost_usd": "0",
                    "live_call_made": False,
                    "settled_at_utc": _now(),
                }
                atomic_json(settlement_path, event, immutable=True)
                settlement_sha256 = sha256_file(settlement_path)
                request.update(
                    {
                        "phase": EVALUATION_PHASE,
                        "phase_settlement_sha256": settlement_sha256,
                    }
                )
                ledger["updated_at_utc"] = _now()
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-phase-settlement-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "settlement_receipt": str(settlement_path),
                    "settlement_receipt_sha256": settlement_sha256,
                }
        finally:
            operation.close()

    @staticmethod
    def _validate_count_error_evidence(
        evidence: dict[str, Any],
        *,
        request: dict[str, Any],
        request_key: str,
        reason: str,
        failure_class: str,
    ) -> bool:
        """Check the reviewed evidence of one count error; say if a retry is on.

        Two reviewed shapes are accepted. The answer-judge evidence of the
        countTokens 404 of 2026-09-15 stays exact, because that review named a
        replacement model rather than a retry. Every other count error is
        reviewed through the general evidence, which names the exact request,
        the exact error, the family and the run, and says whether the free
        count may run again. Only a transient failure may: a permanent one
        repeats until the request or the credential changes.
        """
        if evidence.get("schema") == "arctic-answer-judge-count-error-evidence-v1":
            if (
                evidence
                != {
                    "schema": "arctic-answer-judge-count-error-evidence-v1",
                    "request_key": request_key,
                    "count_tokens_http_status": 404,
                    "live_call_made": False,
                    "replay_prohibited": True,
                    "replacement_model": "gemini-3.1-flash-lite",
                }
                or reason != "HTTPError: HTTP Error 404: Not Found"
            ):
                raise ValueError("the count-error evidence is not exact")
            return False
        retry_authorized = evidence.get("count_retry_authorized")
        if evidence != {
            "schema": COUNT_ERROR_CONTINUATION_EVIDENCE_SCHEMA,
            "request_key": request_key,
            "count_error": reason,
            "count_failure_class": failure_class,
            "live_call_made": False,
            "replay_prohibited": True,
            "count_retry_authorized": retry_authorized,
            "affected_family_id": request.get("family_id"),
            "authorized_run_id": request.get("run_id"),
        } or not isinstance(retry_authorized, bool):
            raise ValueError("the count-error evidence is not exact")
        if retry_authorized and failure_class != TRANSIENT_COUNT_FAILURE:
            raise ValueError("a permanent count error is never counted again")
        return retry_authorized

    def authorize_count_error_continuation(
        self,
        *,
        request_key: str,
        expected_ledger_sha256: str,
        review_file: Path,
        evidence_file: Path,
    ) -> dict[str, Any]:
        """Clear one reviewed pretransport count error without replaying it."""
        operation = hold_operation_lock(self._operation_lock_file)
        try:
            with self._ledger_lock():
                ledger = self._validated_ledger()
                if sha256_file(self.ledger_file) != expected_ledger_sha256:
                    raise ValueError("the reviewed count-error ledger changed")
                request = ledger["requests"].get(request_key)
                final_path = self.receipts_dir / f"{request_key}.json"
                continuation_path = (
                    self.receipts_dir / f"{request_key}.count-error-continuation.json"
                )
                if request is None or not final_path.is_file():
                    raise ValueError("the reviewed count-error request is absent")
                applied_hash = request.get("count_error_continuation_sha256")
                if applied_hash is not None:
                    if (
                        not continuation_path.is_file()
                        or sha256_file(continuation_path) != applied_hash
                    ):
                        raise ValueError("the count-error continuation record changed")
                    return {
                        "schema": "shared-paid-call-count-error-continuation-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "continuation_receipt": str(continuation_path),
                        "continuation_receipt_sha256": applied_hash,
                    }
                final = _read(final_path)
                reason = str(request.get("reason") or "")
                if (
                    request.get("state") != "count_error"
                    or final.get("state") != "count_error"
                    or final.get("live_call_made") is not False
                    or final.get("error") != request.get("reason")
                ):
                    raise ValueError("the request is not a reviewed count error")
                if not review_file.is_file() or not evidence_file.is_file():
                    raise ValueError("the reviewed count-error evidence is absent")
                gate_record = _read(self.execution_gate_file)
                gate = _validate_gate(
                    self.execution_gate_file, str(gate_record.get("allowed_phase"))
                )
                if request.get("gate_sha256") != sha256_file(self.execution_gate_file):
                    raise ValueError("the reviewed count-error gate changed")
                failure_class = _recorded_count_failure_class(final, reason)
                evidence = _read(evidence_file)
                retry_authorized = self._validate_count_error_evidence(
                    evidence,
                    request=request,
                    request_key=request_key,
                    reason=reason,
                    failure_class=failure_class,
                )
                event = {
                    "schema": COUNT_ERROR_CONTINUATION_SCHEMA,
                    "request_key": request_key,
                    "count_error_receipt_sha256": sha256_file(final_path),
                    "count_failure_class": failure_class,
                    "count_retry_authorized": retry_authorized,
                    "ledger_sha256_before": expected_ledger_sha256,
                    "gate_sha256": request["gate_sha256"],
                    "integrated_code_commit": gate["integrated_code_commit"],
                    "review_file": str(review_file.resolve()),
                    "review_file_sha256": sha256_file(review_file),
                    "evidence_file": str(evidence_file.resolve()),
                    "evidence_file_sha256": sha256_file(evidence_file),
                    "live_call_made": False,
                    "replay_prohibited": True,
                    "authorized_at_utc": _now(),
                }
                atomic_json(continuation_path, event, immutable=True)
                continuation_sha256 = sha256_file(continuation_path)
                request["count_error_continuation_sha256"] = continuation_sha256
                if retry_authorized:
                    # The free count of this request may run again. Nothing was
                    # reserved, submitted or charged, so the retry settles no
                    # money; it counts again under its own round and receipts.
                    request["count_failure_class"] = TRANSIENT_COUNT_FAILURE
                unresolved = [
                    value
                    for value in ledger["requests"].values()
                    if value.get("state") == "count_error"
                    and not value.get("count_error_continuation_sha256")
                ]
                if unresolved or int(ledger["inflight"]) != 0:
                    raise ValueError("another count error or inflight request remains")
                ledger["halted"] = False
                ledger["halt_reason"] = None
                ledger["updated_at_utc"] = _now()
                self._validate_ledger(ledger)
                self._validate_immutable_events(ledger)
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-count-error-continuation-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "continuation_receipt": str(continuation_path),
                    "continuation_receipt_sha256": continuation_sha256,
                }
        finally:
            operation.close()

    def _halt(self, reason: str, *, phase: str | None = None) -> None:
        with self._ledger_lock():
            ledger = self._validated_ledger()
            if phase == EVALUATION_PHASE:
                ledger["evaluation_halted"] = True
                ledger["evaluation_halt_reason"] = reason
            else:
                ledger["halted"] = True
                ledger["halt_reason"] = reason
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _mark_not_submitted(
        self,
        request_key: str,
        state: str,
        reason: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._ledger_lock():
            ledger = self._validated_ledger()
            request = ledger["requests"][request_key]
            if request.get("state") != "counting":
                raise ValueError("the paid request is not in its counting state")
            request["state"] = state
            request["reason"] = reason
            request["completed_at_utc"] = _now()
            request.update(extra or {})
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _validate_count_retry_binding(
        self,
        request_key: str,
        request: dict[str, Any],
        base_fields: tuple[str, ...],
    ) -> None:
        """Check the immutable count-error chain a retried count request keeps.

        ``countTokens`` charges nothing, so a request whose free preflight
        failed transiently counts again under a new round rather than ending
        the run. The first count-error receipt stays immutable under the
        request key and every later round keeps its own receipt beside it, so
        the chain proves the retry replaced no paid call and settled no money.
        """
        count_retry_round = request.get("count_retry_round")
        chain_sha256 = request.get("count_retry_from_sha256")
        if count_retry_round is None and chain_sha256 is None:
            return
        original_path = self.receipts_dir / f"{request_key}.json"
        original = _read(original_path) if original_path.is_file() else {}
        stable_fields = tuple(
            name
            for name in base_fields
            if name
            not in {
                "gate_sha256",
                "config_transition_sha256",
                "price_config_sha256",
                "policy_sha256",
            }
        )
        if (
            isinstance(count_retry_round, bool)
            or not isinstance(count_retry_round, int)
            or count_retry_round < 1
            or not isinstance(chain_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", chain_sha256)
            or not original_path.is_file()
            or sha256_file(original_path) != chain_sha256
            or any(original.get(name) != request.get(name) for name in stable_fields)
            or original.get("state") != "count_error"
            or original.get("live_call_made") is not False
        ):
            raise ValueError("a count-retry request lost its count-error receipt")

    def _open_count_retry(
        self, request_key: str, base: dict[str, Any], *, phase: str
    ) -> int:
        """Open a new counting round for a request whose free count failed.

        A new request returns 0, so ``execute`` registers it as usual. A
        request whose count failed transiently, or whose count error a reviewed
        continuation cleared, is reopened here instead of refusing the key:
        nothing was reserved, nothing was submitted and countTokens charges
        nothing, so the free count runs again under its own round and its own
        receipts. A permanent count error still refuses the key, because the
        request or the credential is wrong until a review says otherwise.
        """
        with self._locked_ledger() as ledger:
            request = ledger["requests"].get(request_key)
            if request is None or request.get("state") != "count_error":
                return 0
            final_path = self.receipts_dir / (
                f"{self._request_event_stem(request_key, request)}.json"
            )
            final = _read(final_path) if final_path.is_file() else {}
            recorded = request.get("count_failure_class") or (
                _recorded_count_failure_class(final, str(request.get("reason") or ""))
            )
            if recorded != TRANSIENT_COUNT_FAILURE or not count_error_is_transient(
                final
            ):
                raise ValueError("the paid request key already exists")
            halt = self._phase_halted(ledger, phase)
            if halt is not None:
                raise ValueError(f"the paid-call broker is halted: {halt}")
            stable_fields = (
                "request_key",
                "request_sha256",
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "model",
            )
            if any(request.get(name) != base.get(name) for name in stable_fields):
                raise ValueError("the counted request identity changed")
            original_path = self.receipts_dir / f"{request_key}.json"
            chain_sha256 = request.get("count_retry_from_sha256") or sha256_file(
                original_path
            )
            round_number = int(request.get("count_retry_round") or 0) + 1
            request.update(base)
            request.update(
                {
                    "state": "counting",
                    "count_retry_round": round_number,
                    "count_retry_from_sha256": chain_sha256,
                }
            )
            request.pop("reason", None)
            request.pop("completed_at_utc", None)
            request.pop("count_failure_class", None)
            # ``count_requests`` counts the request keys the ledger holds, not
            # the calls made under them, so a retry round adds none.
            ledger["updated_at_utc"] = _now()
            self._validate_ledger(ledger)
            self._validate_immutable_events(ledger)
            self._commit_ledger(ledger)
            return round_number

    def _record_count_error(
        self,
        request_key: str,
        base: dict[str, Any],
        *,
        event_stem: str,
        error: BaseException,
        failure_class: str,
        attempts: list[dict[str, Any]],
        phase: str,
    ) -> dict[str, Any]:
        """Record one countTokens failure and stop only what it proves.

        No live call was made, nothing was reserved and countTokens is free, so
        the money is never uncertain here. A transient failure that outlived its
        bounded retry therefore halts nothing: the request keeps its immutable
        count-error receipt, the producer records that family and skips it, and
        a later visit counts again. A permanent failure means the request or the
        credential is wrong, so it halts the phase as it always has.
        """
        reason = f"{type(error).__name__}: {error}"
        receipt = {
            **base,
            "state": "count_error",
            "error": reason,
            "count_failure_class": failure_class,
            "count_attempts": attempts,
            "live_call_made": False,
            "completed_at_utc": _now(),
        }
        atomic_json(
            self.receipts_dir / f"{event_stem}.json",
            receipt,
            immutable=True,
        )
        self._mark_not_submitted(
            request_key,
            "count_error",
            reason,
            extra={"count_failure_class": failure_class},
        )
        if failure_class == PERMANENT_COUNT_FAILURE:
            self._halt(f"countTokens error: {type(error).__name__}", phase=phase)
        return receipt

    # The fields that name one request, whatever the configuration bound to it
    # was when it was registered. The gate, the policy, the price config and
    # the transition may all move between one start and the next; the request
    # itself does not.
    REQUEST_IDENTITY_FIELDS = (
        "request_key",
        "request_sha256",
        "run_id",
        "phase",
        "stage",
        "paper_id",
        "family_id",
        "source_version_id",
        "model",
    )

    def _reusable_counting_row(
        self, existing: dict[str, Any], base: dict[str, Any]
    ) -> bool:
        """Say whether a ledger row may be reopened by this count event.

        A ``counting`` row registered a free ``countTokens`` preflight and
        nothing else: no reservation, no submission, no charge, no receipt. A
        start that ends between the count event and the reservation leaves one
        behind, and the next start walks back to that same call. Reusing the
        row costs one more free count and keeps the identity it already has;
        refusing it would end the producer over a call that never happened.

        The identity must be the same. A row whose request key matches but
        whose paper, family, source version, stage or model differs is not this
        request, and is refused like every other existing key.
        """
        if existing.get("state") != "counting":
            return False
        return all(
            existing.get(name) == base.get(name)
            for name in self.REQUEST_IDENTITY_FIELDS
        )

    def _count_event(
        self, request_key: str, base: dict[str, Any], *, phase: str | None = None
    ) -> None:
        with self._locked_ledger() as ledger:
            halt = self._phase_halted(ledger, phase or base.get("phase") or "live_test")
            if halt is not None:
                raise ValueError(f"the paid-call broker is halted: {halt}")
            existing = ledger["requests"].get(request_key)
            if existing is not None and not self._reusable_counting_row(existing, base):
                # The key names a call the ledger already holds in a state that
                # cannot be reopened. Replaying it could charge twice, so it is
                # refused; the refusal is about this call, never about the run.
                raise DuplicateRequestKeyError("the paid request key already exists")
            binding = {
                "paper_id": base["paper_id"],
                "source_version_id": base["source_version_id"],
            }
            old_binding = ledger["family_bindings"].get(base["family_id"])
            if old_binding is not None and old_binding != binding:
                raise PaperBindingConflictError(
                    "the paper family is already bound to another paper or source version"
                )
            for family_id, item in ledger["family_bindings"].items():
                if (
                    item["source_version_id"] == base["source_version_id"]
                    and family_id != base["family_id"]
                ):
                    raise PaperBindingConflictError(
                        "the source version is already bound to another paper family"
                    )
            paper_binding = {
                "family_id": base["family_id"],
                "source_version_id": base["source_version_id"],
            }
            old_paper_binding = ledger["paper_bindings"].get(base["paper_id"])
            if old_paper_binding is not None and old_paper_binding != paper_binding:
                raise PaperBindingConflictError(
                    "the paper ID is already bound to another family or source version"
                )
            ledger["family_bindings"].setdefault(base["family_id"], binding)
            ledger["paper_bindings"].setdefault(base["paper_id"], paper_binding)
            if existing is None:
                # ``count_requests`` is the count of rows, which the ledger
                # totals check against ``len(requests)``. A reused row adds no
                # row, so it adds no count.
                ledger["count_requests"] += 1
            ledger["requests"][request_key] = {**base, "state": "counting"}
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _paper_cost_cap_refusal(self, request_key: str) -> dict[str, Any] | None:
        """Return the stored per-paper cap refusal of one request, or None."""
        with self._ledger_lock():
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if (
                request is None
                or request.get("state") != "not_submitted"
                or request.get("reason") != PAPER_COST_CAP_REASON
            ):
                return None
            receipt = _read(self.receipts_dir / f"{request_key}.json")
            if (
                receipt.get("request_key") != request_key
                or receipt.get("state") != "not_submitted"
                or receipt.get("reason") != PAPER_COST_CAP_REASON
                or receipt.get("live_call_made") is not False
            ):
                raise ValueError("the per-paper cap refusal lost its receipt")
            return receipt

    def _resume_not_submitted(
        self, request_key: str, base: dict[str, Any]
    ) -> int | None:
        with self._locked_ledger() as ledger:
            request = ledger["requests"].get(request_key)
            if request is None:
                return None
            reason = request.get("reason")
            if (
                request.get("state") == "not_submitted"
                and reason == PAPER_COST_CAP_REASON
            ):
                # The cap refusal describes the family, not the moment. It is
                # never resumed and never settled, under any transition.
                raise ValueError("the per-paper cap refusal is never resumed")
            if self._reusable_counting_row(request, base):
                # A free count that never reached a reservation. There is
                # nothing to resume, and ``_count_event`` reopens the row.
                return None
            if (
                request.get("state") != "not_submitted"
                or reason not in RESUMABLE_NOT_SUBMITTED_REASONS
                or request.get("resumed_from_not_submitted_sha256") is not None
            ):
                raise DuplicateRequestKeyError("the paid request key already exists")
            event_path = self._config_transition_event_path
            if event_path is None or not event_path.is_file():
                if reason == AUTHORIZED_CAP_REASON:
                    raise ValueError("the paid request lacks a ceiling extension")
                raise ValueError(
                    "the paid request lacks a reviewed transition to resume under"
                )
            authorization = self._read_transition_event(event_path)["authorization"]
            # The active transition must be the direct successor of the one the
            # request was refused under. The live-test cap needs the ceiling
            # extension; a transient scheduling refusal needs any reviewed
            # transition, because the refusal described the moment, not the
            # request or its budget.
            advanced = (
                request.get("config_transition_sha256") is not None
                and authorization["from_config_transition_sha256"]
                == request.get("config_transition_sha256")
                and base.get("config_transition_sha256")
                == self._config_transition_sha256
                and self._config_transition_sha256
                != request.get("config_transition_sha256")
            )
            if reason == AUTHORIZED_CAP_REASON:
                if not self._is_ceiling_extension(authorization) or not advanced:
                    raise ValueError("the paid request lacks a ceiling extension")
            elif not advanced:
                raise ValueError(
                    "the paid request lacks a reviewed transition to resume under"
                )
            # The request identity is stable. The price and policy hashes are
            # the configuration the resumed request runs under; the transition
            # lineage recorded on the request binds them to the refused one.
            stable_fields = {
                "request_key",
                "request_sha256",
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "model",
            }
            if any(request.get(name) != base.get(name) for name in stable_fields):
                raise ValueError("the paid request resume identity changed")
            original_path = self.receipts_dir / f"{request_key}.json"
            original = _read(original_path) if original_path.is_file() else {}
            exact_input = original.get("input_tokens")
            if (
                original.get("state") != "not_submitted"
                or original.get("reason") != reason
                or original.get("live_call_made") is not False
                or isinstance(exact_input, bool)
                or not isinstance(exact_input, int)
                or exact_input < 0
            ):
                raise ValueError("the paid request resume receipt changed")
            prior_transition = request["config_transition_sha256"]
            request.update(base)
            request.update(
                {
                    "state": "counting",
                    "resumed_from_not_submitted_sha256": sha256_file(original_path),
                    "resumed_from_config_transition_sha256": prior_transition,
                }
            )
            request.pop("reason", None)
            request.pop("completed_at_utc", None)
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)
            return exact_input

    def _pace(self) -> None:
        """Wait until the frozen per-minute submission window has room.

        The active phase is read from ``_pacing_phase``, which ``execute`` sets
        before the wait, so the method keeps its historical one-argument shape.
        """
        phase = self._pacing_phase
        while True:
            with self._ledger_lock():
                ledger = self._validated_ledger()
            cutoff = datetime.now(UTC) - timedelta(minutes=1)
            if phase == EVALUATION_PHASE:
                recent = self._evaluation_recent_submission_times(ledger, cutoff)
            else:
                recent = sorted(
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    for value in ledger["recent_submission_times_utc"]
                    if datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
                )
            limit = self._minute_limit(phase)
            if len(recent) < limit:
                return
            wait_seconds = max(
                (recent[0] + timedelta(minutes=1) - datetime.now(UTC)).total_seconds(),
                0,
            )
            if wait_seconds:
                time.sleep(min(wait_seconds + 0.01, 60.0))

    def _minute_limit(self, phase: str) -> int:
        if phase == EVALUATION_PHASE:
            self._require_evaluation()
            return int(self.evaluation_policy["maximum_requests_per_minute"])  # type: ignore[index]
        return int(self.policy["maximum_generation_requests_per_minute"])

    def _concurrency_limit(self, phase: str) -> int:
        if phase == EVALUATION_PHASE:
            self._require_evaluation()
            return int(self.evaluation_policy["maximum_concurrent_requests"])  # type: ignore[index]
        return int(self.policy["maximum_concurrent_generation_requests"])

    def _check_evaluation_reservation(
        self, ledger: dict[str, Any], request: dict[str, Any], reserved: Decimal
    ) -> None:
        """Apply the evaluation ceiling, the reserve and the lifetime ceiling."""
        self._require_evaluation()
        policy = self.evaluation_policy or {}
        if reserved > _money(policy["maximum_request_reserved_cost_usd"], "request"):
            raise ValueError(PER_REQUEST_CAP_REASON)
        evaluation = self._evaluation_totals(ledger)
        if evaluation["used_usd"] + reserved > _money(
            policy["evaluation_ceiling_usd"], "evaluation ceiling"
        ):
            raise ValueError(EVALUATION_CEILING_REASON)
        if evaluation["used_usd"] + reserved > _money(
            self.policy["reserved_for_benchmark_evaluation_usd"], "evaluation reserve"
        ):
            raise ValueError("the paid request exceeds the evaluation reserve")
        all_used = sum(
            _money(ledger[name], name)
            for name in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
        )
        if self.prior + all_used + reserved > _money(
            self.policy["project_lifetime_ceiling_usd"], "lifetime"
        ):
            raise ValueError("the paid request exceeds the project lifetime ceiling")
        trial = request.get("evaluation_trial") or {}
        trial_key = canonical_json(
            {
                "family_id": request.get("family_id"),
                "stage": request.get("stage"),
                "condition": trial.get("condition"),
                "arm": trial.get("arm"),
            }
        )
        if evaluation["calls_by_trial_key"].get(trial_key, 0) >= int(
            policy["maximum_calls_per_item_condition_model_arm"]
        ):
            raise ValueError(EVALUATION_ITEM_REPEAT_REASON)

    def _reserve(
        self,
        *,
        request_key: str,
        phase: str,
        paper_id: str,
        family_id: str,
        stage: str,
        reserved: Decimal,
    ) -> None:
        with self._ledger_lock():
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if not request or request.get("state") != "counting":
                raise ValueError("the paid request is not ready for reservation")
            continuation_events = self._ambiguous_continuation_events(ledger)
            ambiguous_requests = {
                key
                for key, row in ledger["requests"].items()
                if row.get("state") == "ambiguous_charge"
            }
            unresolved_ambiguous = ambiguous_requests - continuation_events.keys()
            blocking = {
                key
                for key in unresolved_ambiguous
                if phase == EVALUATION_PHASE
                or ledger["requests"][key].get("phase") != EVALUATION_PHASE
            }
            if self._phase_halted(ledger, phase) is not None or blocking:
                raise ValueError("the paid-call broker is halted")
            evaluation_used = self._evaluation_totals(ledger)["used_usd"]
            evaluation_submissions = self._evaluation_totals(ledger)["submissions"]
            if phase == EVALUATION_PHASE:
                self._check_evaluation_reservation(ledger, request, reserved)
            else:
                self._check_construction_reservation(
                    ledger,
                    reserved,
                    evaluation_used=evaluation_used,
                    evaluation_submissions=evaluation_submissions,
                )
            paper = ledger["papers"].setdefault(
                family_id,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "reserved_usd": "0",
                    "spent_usd": "0",
                    "ambiguous_usd": "0",
                    "paper_id": paper_id,
                    "source_version_id": request["source_version_id"],
                },
            )
            paper_used = sum(
                _money(paper[name], name)
                for name in ("reserved_usd", "spent_usd", "ambiguous_usd")
            )
            # An evaluation item is its own paper family; its repeat limit
            # replaces the per-paper construction cap.
            if phase != EVALUATION_PHASE and paper_used + reserved > _money(
                self.policy["maximum_paper_cost_usd"], "paper"
            ):
                raise ValueError(PAPER_COST_CAP_REASON)
            if phase == "live_test":
                live_used = sum(
                    _money(row.get("reserved_usd", 0), "live reserved")
                    + _money(row.get("spent_usd", 0), "live spent")
                    + _money(row.get("ambiguous_usd", 0), "live ambiguous")
                    for row in ledger["live_test_papers"].values()
                )
                live_test_cap = _money(
                    self.policy["live_test_suballocation_usd"], "live test"
                )
                if self._authorized_live_test_ceiling_usd is not None:
                    live_test_cap = min(
                        live_test_cap, self._authorized_live_test_ceiling_usd
                    )
                if live_used + reserved > live_test_cap:
                    raise ValueError(
                        "the paid request exceeds the authorized live-test cap"
                    )
                paper_limit = self.policy["live_test_maximum_papers"]
                if (
                    paper_limit is not None
                    and family_id not in ledger["live_test_papers"]
                    and len(ledger["live_test_papers"]) >= int(paper_limit)
                ):
                    raise ValueError("the live test reached its paper limit")
                live_submissions = sum(
                    int(row.get("submissions", 0))
                    for row in ledger["live_test_papers"].values()
                )
                submission_limit = self.policy[
                    "live_test_maximum_generation_submissions"
                ]
                if submission_limit is not None and live_submissions >= int(
                    submission_limit
                ):
                    raise ValueError("the live-test submission limit is complete")
            if self._phase_inflight(ledger, phase) >= self._concurrency_limit(phase):
                raise ValueError(CONCURRENCY_LIMIT_REASON)
            cutoff = datetime.now(UTC) - timedelta(minutes=1)
            # Each phase owns its minute window as well as its slots. The
            # construction window is `recent_submission_times_utc`; the
            # evaluation window is read from the submission times of the
            # evaluation requests. The evaluation limit is higher than the
            # construction limit, so a shared window would let evaluation
            # calls refuse construction calls on the pace alone.
            if phase == EVALUATION_PHASE:
                recent_count = len(
                    self._evaluation_recent_submission_times(ledger, cutoff)
                )
            else:
                recent_count = sum(
                    1
                    for value in ledger["recent_submission_times_utc"]
                    if datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
                )
            if recent_count >= self._minute_limit(phase):
                raise ValueError(MINUTE_LIMIT_REASON)
            if phase != EVALUATION_PHASE:
                ledger["recent_submission_times_utc"] = [
                    value
                    for value in ledger["recent_submission_times_utc"]
                    if datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
                ] + [_now()]
            ledger["reserved_usd"] = str(
                _money(ledger["reserved_usd"], "reserved") + reserved
            )
            ledger["generation_submissions"] += 1
            ledger["inflight"] += 1
            paper["reserved_usd"] = str(
                _money(paper["reserved_usd"], "paper reserved") + reserved
            )
            stage_row = ledger["stages"].setdefault(
                stage,
                {
                    "submissions": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "reserved_usd": "0",
                    "spent_usd": "0",
                    "ambiguous_usd": "0",
                },
            )
            stage_row["submissions"] += 1
            stage_row["reserved_usd"] = str(
                _money(stage_row["reserved_usd"], "stage reserved") + reserved
            )
            if phase == "live_test":
                live = ledger["live_test_papers"].setdefault(
                    family_id,
                    {
                        "submissions": 0,
                        "reserved_usd": "0",
                        "spent_usd": "0",
                        "ambiguous_usd": "0",
                    },
                )
                live["submissions"] += 1
                live["reserved_usd"] = str(
                    _money(live["reserved_usd"], "live reserved") + reserved
                )
            request.update(
                {
                    "state": "submitted",
                    "phase": phase,
                    "reserved_usd": str(reserved),
                    "submitted_at_utc": _now(),
                }
            )
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _check_construction_reservation(
        self,
        ledger: dict[str, Any],
        reserved: Decimal,
        *,
        evaluation_used: Decimal,
        evaluation_submissions: int,
    ) -> None:
        """Apply the construction caps; evaluation liabilities never count here."""
        if reserved > _money(
            self.policy["maximum_request_reserved_cost_usd"], "request"
        ):
            raise ValueError(PER_REQUEST_CAP_REASON)
        used = (
            sum(
                _money(ledger[name], name)
                for name in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
            )
            - evaluation_used
        )
        if used + reserved > _money(
            self.policy["away_session_total_ceiling_usd"], "away"
        ):
            raise ValueError("the paid request exceeds the authorized away cap")
        construction_used = self.prior + used
        if construction_used + reserved > _money(
            self.policy["construction_review_checkpoint_usd"], "checkpoint"
        ):
            raise ValueError("the paid request exceeds the construction checkpoint")
        if construction_used + evaluation_used + reserved > _money(
            self.policy["project_lifetime_ceiling_usd"], "lifetime"
        ):
            raise ValueError("the paid request exceeds the project lifetime ceiling")
        if ledger["generation_submissions"] - evaluation_submissions >= int(
            self.policy["away_maximum_generation_submissions"]
        ):
            raise ValueError("the away-session submission limit is complete")
        if ledger["accepted_question_count"] >= int(
            self.policy["accepted_question_target"]
        ):
            raise ValueError("the accepted-question target is complete")

    def _settle(
        self,
        request_key: str,
        *,
        actual: Decimal | None,
        usage: dict[str, int] | None,
    ) -> bool:
        """Settle one submitted request, and say whether it settled here.

        A request that is not ``submitted`` holds no reservation this call can
        release: it was never submitted, or another worker of the same ledger
        already settled it and owns that accounting. Nothing is owed either
        way, so the settlement records the skip and returns ``False`` instead
        of ending the run. A settlement that ended the run this way stopped the
        chapter 3 producer at 13:53 UTC on 2026-09-16.
        """
        with self._ledger_lock():
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            state = None if request is None else request.get("state")
            if state == "submitted":
                self._apply_settlement(ledger, request, actual=actual, usage=usage)
                self._commit_ledger(ledger)
        if state != "submitted":
            self._record_settle_skipped(
                request_key,
                state=state,
                reason="the request was not submitted when the settlement ran",
            )
            return False
        return True

    def _apply_settlement(
        self,
        ledger: dict[str, Any],
        request: dict[str, Any],
        *,
        actual: Decimal | None,
        usage: dict[str, int] | None,
    ) -> None:
        """Move one submitted request's money into the open ledger."""
        reserved = _money(request["reserved_usd"], "reservation", positive=True)
        paper = ledger["papers"][request["family_id"]]
        stage = ledger["stages"][request["stage"]]
        ledger["reserved_usd"] = str(
            _money(ledger["reserved_usd"], "reserved") - reserved
        )
        paper["reserved_usd"] = str(
            _money(paper["reserved_usd"], "paper reserved") - reserved
        )
        stage["reserved_usd"] = str(
            _money(stage["reserved_usd"], "stage reserved") - reserved
        )
        live = (
            ledger["live_test_papers"].get(request["family_id"])
            if request["phase"] == "live_test"
            else None
        )
        if live:
            live["reserved_usd"] = str(
                _money(live["reserved_usd"], "live reserved") - reserved
            )
        if actual is None:
            amount = reserved
            ledger["ambiguous_reserved_usd"] = str(
                _money(ledger["ambiguous_reserved_usd"], "ambiguous") + amount
            )
            paper["ambiguous_usd"] = str(
                _money(paper["ambiguous_usd"], "paper ambiguous") + amount
            )
            stage["ambiguous_usd"] = str(
                _money(stage["ambiguous_usd"], "stage ambiguous") + amount
            )
            if live:
                live["ambiguous_usd"] = str(
                    _money(live["ambiguous_usd"], "live ambiguous") + amount
                )
            request["state"] = "ambiguous_charge"
            if request["phase"] == EVALUATION_PHASE:
                # Halt the evaluation phase only. The construction phase
                # keeps its own ceiling, slots and window.
                ledger["evaluation_halted"] = True
                ledger["evaluation_halt_reason"] = AMBIGUOUS_HALT_REASON
            else:
                ledger["halted"] = True
                ledger["halt_reason"] = AMBIGUOUS_HALT_REASON
        else:
            actual = _money(actual, "actual cost")
            if actual > reserved:
                raise ValueError("actual cost exceeds the paid request reservation")
            ledger["spent_usd"] = str(_money(ledger["spent_usd"], "spent") + actual)
            paper["spent_usd"] = str(_money(paper["spent_usd"], "paper spent") + actual)
            stage["spent_usd"] = str(_money(stage["spent_usd"], "stage spent") + actual)
            if live:
                live["spent_usd"] = str(
                    _money(live["spent_usd"], "live spent") + actual
                )
            if usage:
                stage["input_tokens"] += usage["promptTokenCount"]
                stage["output_tokens"] += usage["candidatesTokenCount"]
                stage["thinking_tokens"] += usage["thoughtsTokenCount"]
                paper["input_tokens"] += usage["promptTokenCount"]
                paper["output_tokens"] += usage["candidatesTokenCount"]
                paper["thinking_tokens"] += usage["thoughtsTokenCount"]
            request["state"] = "completed"
            request["actual_cost_usd"] = str(actual)
            request["usage"] = usage
        ledger["inflight"] = max(int(ledger["inflight"]) - 1, 0)
        request["completed_at_utc"] = _now()
        ledger["updated_at_utc"] = _now()

    def _request_row(self, request_key: str) -> dict[str, Any] | None:
        """Read one request row through the ledger lock, or None."""
        with self._ledger_lock():
            ledger = self._validated_ledger()
            row = ledger["requests"].get(request_key)
            return dict(row) if row is not None else None

    def _record_settle_skipped(
        self, request_key: str, *, state: str | None, reason: str
    ) -> None:
        """Record one settlement or recovery that moved nothing, and continue.

        The note is an observation beside the receipts, never an accounting
        event: a request that is not submitted holds no money, and a request
        another worker settled has its money in the row that worker wrote. A
        later skip of the same request rewrites the note, so it always states
        the last observation.
        """
        event = {
            "schema": SETTLE_SKIPPED_SCHEMA,
            "request_key": request_key,
            "observed_state": state,
            "reason": reason,
            "recorded_at_utc": _now(),
        }
        try:
            atomic_json(self.receipts_dir / f"{request_key}.settle-skipped.json", event)
        except Exception:
            # The note can never become a second failure path: a recovery that
            # correctly settles nothing must still return.
            pass

    def _completed_receipt(
        self, submitted: dict[str, Any], response: Any
    ) -> tuple[dict[str, Any], Decimal | None, dict[str, int] | None]:
        if (
            is_evaluation_stage(submitted.get("stage"))
            and self.evaluation_config is None
        ):
            # Fail closed: a broker without the evaluation price config cannot
            # settle an evaluation response, and must not record it as an
            # ambiguous charge either.
            raise ValueError(
                "an evaluation response needs the benchmark evaluation price config"
            )
        try:
            usage = _normalized_usage(response)
            values = [
                usage[name]
                for name in (
                    "promptTokenCount",
                    "candidatesTokenCount",
                    "thoughtsTokenCount",
                    "totalTokenCount",
                )
            ]
            request_config = self.config_for_stage(str(submitted.get("stage") or ""))
            if submitted.get("model") != request_config["model"]:
                raise ValueError("the submitted request model changed")
            actual = _cost(request_config, values[0], values[1] + values[2])
            if actual > _money(submitted["reserved_usd"], "reservation", positive=True):
                raise ValueError("provider usage exceeds the reservation")
        except Exception as error:
            return (
                {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error": f"{type(error).__name__}: {error}",
                    "response": response,
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                },
                None,
                None,
            )
        return (
            {
                **submitted,
                "state": "completed",
                "actual_cost_usd": str(actual),
                "usage": usage,
                "response": response,
                "live_call_made": True,
                "completed_at_utc": _now(),
            },
            actual,
            usage,
        )

    def _recover_orphans(
        self, *, active_run_id: str | None = None, own_run_only: bool = False
    ) -> None:
        """Settle every interrupted request, then return.

        ``own_run_only`` limits the recovery to the active run. A concurrent
        evaluation request does not hold the exclusive operation lock, so a
        construction run of another run id can be inside a live call with a
        durable response written and its settlement still pending. Recovering
        that request would settle it twice. The evaluation path therefore
        recovers only its own run, whose in-flight locks it can see.
        """
        proved = self._custody_proved
        with self._ledger_lock():
            ledger = self._validated_ledger()
            # Only the rows this pass can act on are copied. A terminal row
            # whose custody was proved needs nothing more, and copying all
            # 8,230 of them, with the two receipt paths of each, cost 50 ms of
            # every paid call under the exclusive operation lock.
            requests = {
                key: dict(row)
                for key, row in ledger["requests"].items()
                if key not in proved or row["state"] not in TERMINAL_REQUEST_STATES
            }
        for request_key, request in requests.items():
            event_stem = self._request_event_stem(request_key, request)
            final_path = self.receipts_dir / f"{event_stem}.json"
            received_path = self.receipts_dir / f"{event_stem}.received.json"
            if request["state"] in TERMINAL_REQUEST_STATES:
                # Every terminal request keeps its custody check, whichever
                # run wrote it. A final receipt is immutable and is never
                # removed, so a row that was proved to have one still has
                # one; stating all 6,000 of them again on every paid call
                # cost 95 ms a call and proved nothing new.
                if request_key in proved:
                    continue
                if not final_path.is_file():
                    error = ValueError(
                        "a terminal paid request lacks its immutable final receipt"
                    )
                    self._record_integrity_halt(error)
                    raise error
                proved.add(request_key)
                continue
            if request["state"] != "submitted":
                continue
            if (
                own_run_only
                and active_run_id is not None
                and request.get("run_id") != active_run_id
            ):
                continue
            if self._inflight_held(request_key):
                # A live worker holds this request's in-flight lock: the
                # provider call is still running. It is not an orphan.
                continue
            if (
                active_run_id is not None
                and request.get("run_id") != active_run_id
                and not final_path.is_file()
                and not received_path.is_file()
            ):
                # A stopped producer can leave one submitted request without a
                # durable response. Keep that liability reserved. A different
                # reviewed run can use the second concurrency slot without
                # replaying or settling the old request.
                continue
            # The snapshot above is a read, not a hold. A worker of another run
            # settles its own request without this process's lock, so the row
            # can have moved past `submitted` since the snapshot. Read it again
            # before any receipt is written.
            current = self._request_row(request_key)
            if current is None or current.get("state") != "submitted":
                self._record_settle_skipped(
                    request_key,
                    state=None if current is None else current.get("state"),
                    reason="the request left its submitted state during recovery",
                )
                continue
            if final_path.is_file():
                receipt = _read(final_path)
                state = receipt.get("state")
                if state == "completed":
                    actual = _money(
                        receipt.get("actual_cost_usd"), "recovered actual cost"
                    )
                    usage = receipt.get("usage")
                elif state == "ambiguous_charge":
                    actual = None
                    usage = None
                else:
                    raise ValueError("an orphan final receipt has an invalid state")
            elif received_path.is_file():
                received = _read(received_path)
                if received.get("request_key") != request_key:
                    raise ValueError("a received provider response has the wrong key")
                receipt, actual, usage = self._completed_receipt(
                    {
                        **request,
                        "state": "submitted",
                        "input_tokens": received.get("input_tokens"),
                    },
                    received.get("response"),
                )
                if not self._write_recovered_receipt(request_key, final_path, receipt):
                    continue
            else:
                actual = None
                usage = None
                receipt = {
                    **request,
                    "state": "ambiguous_charge",
                    "error": "interrupted request has no durable provider response",
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                if not self._write_recovered_receipt(request_key, final_path, receipt):
                    continue
            # A settlement that finds the row already moved records the skip
            # and returns False. Its accounting belongs to the other worker,
            # so this recovery owes nothing and the run continues.
            self._settle(request_key, actual=actual, usage=usage)

    def _write_recovered_receipt(
        self, request_key: str, final_path: Path, receipt: dict[str, Any]
    ) -> bool:
        """Write one recovered final receipt, or report the other writer."""
        try:
            atomic_json(final_path, receipt, immutable=True)
        except FileExistsError:
            self._record_settle_skipped(
                request_key,
                state=(self._request_row(request_key) or {}).get("state"),
                reason="another worker wrote the final receipt during recovery",
            )
            return False
        return True

    def execute(
        self,
        *,
        phase: str,
        run_id: str,
        stage: str,
        paper_id: str,
        family_id: str,
        source_version_id: str,
        request_key: str,
        payload: dict[str, Any],
        trial: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if sha256_file(self.price_config_file) != self.active_price_config_sha256:
            raise ValueError("the active price configuration changed after startup")
        if phase not in PHASES or not stage_supported(stage):
            raise ValueError("the paid request phase or stage is unsupported")
        if (phase == EVALUATION_PHASE) != is_evaluation_stage(stage):
            raise ValueError("the paid request phase does not match its stage family")
        if phase == EVALUATION_PHASE:
            self._require_evaluation()
            if sha256_file(self.evaluation_price_config_file) != (  # type: ignore[arg-type]
                self.active_evaluation_price_config_sha256
            ):
                raise ValueError(
                    "the evaluation price configuration changed after startup"
                )
            if not isinstance(trial, dict) or not str(trial.get("trial_id") or ""):
                raise ValueError("an evaluation request needs its trial record")
        elif trial is not None:
            raise ValueError("a construction request cannot carry a trial record")
        if not all((run_id, paper_id, family_id, source_version_id, request_key)):
            raise ValueError("the paid request identity is incomplete")
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid request key must be a lowercase SHA-256 value")
        request_config = self.config_for_stage(stage)
        timeout_seconds = self._timeout_for_stage(stage)
        _validate_payload(payload, request_config)
        expected_key = broker_request_key(
            model=request_config["model"],
            run_id=run_id,
            phase=phase,
            stage=stage,
            paper_id=paper_id,
            family_id=family_id,
            source_version_id=source_version_id,
            payload=payload,
            trial_id=str(trial["trial_id"]) if trial else None,
        )
        if request_key != expected_key:
            raise ValueError("the paid request key does not bind the exact request")
        request_hash = sha256_bytes(canonical_json(payload).encode())
        base = {
            "request_key": request_key,
            "request_sha256": request_hash,
            # The phase belongs to the request from its first record, not from
            # its reservation. A refusal that stops before the reservation used
            # to carry no phase, and `_only_evaluation_activity_since` reads a
            # row without a phase as a construction request. One such refusal
            # of the evaluation phase halted the whole shared ledger on
            # 2026-09-16 at 23:03:56 UTC.
            "phase": phase,
            "run_id": run_id,
            "stage": stage,
            "paper_id": paper_id,
            "family_id": family_id,
            "source_version_id": source_version_id,
            "model": request_config["model"],
            "gate_sha256": sha256_file(
                self.evaluation_gate_file  # type: ignore[arg-type]
                if phase == EVALUATION_PHASE
                else self.execution_gate_file
            ),
            "price_config_sha256": self.active_price_config_sha256,
            "policy_sha256": sha256_file(self.policy_file),
            "config_transition_sha256": self._config_transition_sha256,
            "timeout_seconds": timeout_seconds,
        }
        if phase == EVALUATION_PHASE:
            base.update(
                {
                    "evaluation_trial": {
                        name: trial.get(name)  # type: ignore[union-attr]
                        for name in (
                            "trial_id",
                            "eval_set_id",
                            "item_id",
                            "condition",
                            "arm",
                            "repeat",
                        )
                    },
                    "evaluation_policy_sha256": sha256_file(
                        self.evaluation_policy_file  # type: ignore[arg-type]
                    ),
                    "evaluation_price_config_sha256": (
                        self.active_evaluation_price_config_sha256
                    ),
                    "evaluation_gate_sha256": base["gate_sha256"],
                    "evaluation_policy_transition_sha256": (
                        self._evaluation_transition_sha256
                    ),
                }
            )
        # Admission is serialised; the live call is not. Every request
        # serialises its admission (recovery, gate check, pace, count,
        # reserve), then releases the admission and holds only its own
        # in-flight lock during the live call. So N calls run at once and
        # every ledger write stays under the ledger lock.
        #
        # An evaluation request never takes the exclusive operation lock. A
        # construction request takes it, because a reviewed operation of this
        # ledger must not overlap the accounting of a paid request.
        #
        # A sequential construction request holds one lock for the whole call,
        # as it always has. A concurrent construction request holds it only for
        # the ledger mutations themselves: the orphan recovery, the reservation
        # and the receipt it makes durable. The free token count, its retries
        # and the pacing wait stay outside, because none of them touches the
        # accounting and each of them can take minutes. Holding the pacing
        # sleep and the count inside the lock made every peer thread queue
        # behind them and faulted 19 papers in 40 minutes on 2026-09-16.
        concurrent = phase == EVALUATION_PHASE or self.concurrent_construction
        operation = None
        inflight_lock = None
        reserved_operation = None
        admitted = False
        if phase != EVALUATION_PHASE and not self.concurrent_construction:
            # This is the broker's one ordinary request path, so it waits for
            # a reviewed operation of this ledger rather than ending the run on
            # it. The bound raises ``BrokerOperationBusyError``, which the
            # producer contains against one family like any other fault.
            operation = self._acquire_operation_lock("whole_call", request_key)
            self._whole_call_operation[threading.get_ident()] = operation
        if concurrent:
            self._admission_lock.acquire()
            admitted = True
        if concurrent:
            self._flush_state.deferred = True
        try:
            if exclusive_batch_marker_path(self.ledger_file).exists():
                raise ValueError("exclusive Gemini batch mode is active")
            with self._exclusive_operation("orphan_recovery", request_key):
                self._recover_orphans(active_run_id=run_id, own_run_only=concurrent)
            if phase == EVALUATION_PHASE:
                gate = _validate_evaluation_gate(self.evaluation_gate_file)  # type: ignore[arg-type]
                self._validate_evaluation_gate_hashes(gate)
                self._validate_evaluation_binding(
                    gate, self._evaluation_binding, request_run_id=run_id
                )
            else:
                gate = _validate_gate(self.execution_gate_file, phase)
                self._validate_stream_input_binding(
                    gate, self._stream_input_binding, request_run_id=run_id
                )
            refusal = self._paper_cost_cap_refusal(request_key)
            if refusal is not None:
                # The per-paper cost cap refusal is final for that family: it
                # is never resumed and never settled. The stored refusal is
                # replayed free, so the producer records the family and
                # continues with the next paper.
                return refusal
            self._pacing_phase = phase
            self._pace()
            client = self.transport or GeminiTransport(
                self.config["api_base"],
                _load_key(self.credential_file),
                timeout=timeout_seconds,
            )
            # A request whose free count failed transiently counts again here,
            # before the resume path, which knows only the states a reservation
            # can reach.
            # One exclusive operation, one shared ledger lock and one read of
            # the ledger for all three steps that register a counted request.
            with (
                self._exclusive_operation("count_registration", request_key),
                self._ledger_session(),
            ):
                count_retry_round = self._open_count_retry(
                    request_key, base, phase=phase
                )
                exact_input = (
                    None
                    if count_retry_round
                    else self._resume_not_submitted(request_key, base)
                )
                resumed = exact_input is not None
                if exact_input is None and not count_retry_round:
                    self._count_event(request_key, base, phase=phase)
            count_attempts: list[dict[str, Any]] = []
            if exact_input is None:
                count_stem = _count_event_stem(request_key, count_retry_round)
                if admitted:
                    # The free token count is a provider round trip that
                    # charges nothing and touches no accounting. Holding the
                    # admission through it made every peer wait for a network
                    # call that is not theirs, and the admission is what sets
                    # how many calls can be in flight. The count is already
                    # registered in the ledger, so a peer that admits while
                    # this one counts meets a request that is counting and
                    # nothing else.
                    self._admission_lock.release()
                    admitted = False
                for attempt in range(1, COUNT_RETRY_ATTEMPTS + 1):
                    started_at = _now()
                    try:
                        counted = client.post(
                            request_config["model"],
                            "countTokens",
                            {
                                "generateContentRequest": {
                                    "model": f"models/{request_config['model']}",
                                    **payload,
                                }
                            },
                        )
                        value = counted["totalTokens"]
                        if (
                            isinstance(value, bool)
                            or not isinstance(value, int)
                            or value < 0
                        ):
                            raise ValueError(
                                "countTokens did not return a nonnegative integer"
                            )
                    except Exception as error:
                        failure_class = _count_failure_class(error)
                        count_attempts.append(
                            {
                                "attempt": attempt,
                                "started_at_utc": started_at,
                                "completed_at_utc": _now(),
                                "error": f"{type(error).__name__}: {error}",
                                "http_status": _count_http_status(error),
                                "failure_class": failure_class,
                            }
                        )
                        if (
                            failure_class == PERMANENT_COUNT_FAILURE
                            or attempt == COUNT_RETRY_ATTEMPTS
                        ):
                            if concurrent and not admitted:
                                self._admission_lock.acquire()
                                admitted = True
                            with self._exclusive_operation("count_error", request_key):
                                return self._record_count_error(
                                    request_key,
                                    base,
                                    event_stem=count_stem,
                                    error=error,
                                    failure_class=failure_class,
                                    attempts=count_attempts,
                                    phase=phase,
                                )
                        # The free count charges nothing, so waiting for the
                        # provider costs the run nothing but the wait.
                        time.sleep(_count_retry_delay(attempt))
                        continue
                    count_attempts.append(
                        {
                            "attempt": attempt,
                            "started_at_utc": started_at,
                            "completed_at_utc": _now(),
                            "error": None,
                            "http_status": None,
                            "failure_class": None,
                        }
                    )
                    exact_input = value
                    break
                if concurrent and not admitted:
                    # Back into the admission for the reservation, which is
                    # accounting and is serialised.
                    self._admission_lock.acquire()
                    admitted = True
            if count_retry_round:
                event_stem = _count_event_stem(request_key, count_retry_round)
            elif resumed:
                event_stem = f"{request_key}.resume-{self._config_transition_sha256}"
            else:
                event_stem = request_key
            count_record = (
                {"count_attempts": count_attempts} if len(count_attempts) > 1 else {}
            )
            if exact_input > int(request_config["maximum_input_tokens"]):
                receipt = {
                    **base,
                    "state": "too_large_not_ready",
                    "input_tokens": exact_input,
                    "live_call_made": False,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                with self._exclusive_operation("too_large_halt", request_key):
                    self._mark_not_submitted(
                        request_key,
                        "too_large_not_ready",
                        "counted request exceeds the model input limit",
                    )
                    self._halt(
                        "counted request exceeds the model input limit", phase=phase
                    )
                return receipt
            output_limit = int(payload["generationConfig"]["maxOutputTokens"])
            reserved = _cost(request_config, exact_input, output_limit)
            deadline = time.monotonic() + TRANSIENT_RESERVATION_RETRY_SECONDS
            # The reservation is the accounting itself, so the exclusive
            # operation lock is held across it and stays held through the
            # durable submitted receipt and the in-flight lock: a peer's orphan
            # recovery must never meet a reserved request with no in-flight
            # lock. The wait between two attempts is not accounting, so the
            # lock is released for it.
            while True:
                if operation is None and reserved_operation is None:
                    reserved_operation = self._acquire_operation_lock(
                        "reserve", request_key
                    )
                    if exclusive_batch_marker_path(self.ledger_file).exists():
                        raise ValueError("exclusive Gemini batch mode is active")
                try:
                    self._reserve(
                        request_key=request_key,
                        phase=phase,
                        paper_id=paper_id,
                        family_id=family_id,
                        stage=stage,
                        reserved=reserved,
                    )
                    break
                except ValueError as error:
                    if (
                        str(error) in TRANSIENT_RESERVATION_REASONS
                        and time.monotonic() < deadline
                    ):
                        # Another request of this phase holds the slot or the
                        # window; the request is counted and waits for room.
                        if reserved_operation is not None:
                            self._release_operation_lock(reserved_operation)
                            reserved_operation = None
                        time.sleep(TRANSIENT_RESERVATION_RETRY_INTERVAL_SECONDS)
                        continue
                    receipt = {
                        **base,
                        "state": "not_submitted",
                        "input_tokens": exact_input,
                        "reason": str(error),
                        "live_call_made": False,
                        "completed_at_utc": _now(),
                    }
                    atomic_json(
                        self.receipts_dir / f"{event_stem}.json",
                        receipt,
                        immutable=True,
                    )
                    self._mark_not_submitted(request_key, "not_submitted", str(error))
                    return receipt
            submitted = {
                **base,
                **count_record,
                "state": "submitted",
                "input_tokens": exact_input,
                "reserved_usd": str(reserved),
                "submitted_at_utc": _now(),
            }
            if concurrent:
                # The request is reserved and durable. Hold its own in-flight
                # lock, then release the admission and the exclusive operation
                # lock so the next request can be admitted while this one is
                # on the wire. Orphan recovery skips a request whose in-flight
                # lock is held.
                inflight_lock = self._hold_inflight(request_key)
                if admitted:
                    self._admission_lock.release()
                    admitted = False
                if reserved_operation is not None:
                    self._release_operation_lock(reserved_operation)
                    reserved_operation = None
                if operation is not None:
                    self._whole_call_operation.pop(threading.get_ident(), None)
                    self._release_operation_lock(operation)
                    operation = None
                # The reservation is durable before the provider call, which
                # is the money rule, and the flush is here rather than inside
                # the admission so that the reservations of every concurrent
                # call meet in one fsync.
                self._flush_state.deferred = False
                self._store.flush()
            # The submitted receipt is written after the admission, not inside
            # it. The in-flight lock is already held, so orphan recovery leaves
            # this request alone, and a durable write costs 150 to 470 ms on
            # this data disk: inside the admission every peer paid for it.
            atomic_json(
                self.receipts_dir / f"{event_stem}.submitted.json",
                submitted,
                immutable=True,
            )
            # Observability is deliberately outside the accounting transaction.
            # A missing trace must not change request dispatch or ledger state.
            try:
                record_model_request_trace(
                    self.receipts_dir,
                    identity=base,
                    payload=payload,
                    submitted_at_utc=submitted["submitted_at_utc"],
                )
            except Exception:
                # The trace is explicitly non-authoritative. Even an unexpected
                # observability failure cannot strand a paid reservation.
                pass
            try:
                response = client.post(
                    request_config["model"], "generateContent", payload
                )
            except urllib.error.HTTPError as error:
                error_body = _http_error_body(error)
                receipt = {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error_class": "known_http_response_unknown_charge",
                    "http_status": error.code,
                    "retry_after": error.headers.get("Retry-After")
                    if error.headers
                    else None,
                    "error_body": error_body,
                    "provider_error_status": _provider_error_status(error_body),
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                self._settle(request_key, actual=None, usage=None)
                return receipt
            except Exception as error:
                receipt = {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error": f"{type(error).__name__}: provider outcome unknown",
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                self._settle(request_key, actual=None, usage=None)
                return receipt
            received = {
                **submitted,
                "state": "response_received",
                "response": response,
                "live_call_made": True,
                "received_at_utc": _now(),
            }
            atomic_json(
                self.receipts_dir / f"{event_stem}.received.json",
                received,
                immutable=True,
            )
            receipt, actual, usage = self._completed_receipt(submitted, response)
            atomic_json(
                self.receipts_dir / f"{event_stem}.json",
                receipt,
                immutable=True,
            )
            self._settle(request_key, actual=actual, usage=usage)
            return receipt
        finally:
            if getattr(self._flush_state, "deferred", False):
                self._flush_state.deferred = False
                self._store.flush()
            if inflight_lock is not None:
                fcntl.flock(inflight_lock, fcntl.LOCK_UN)
                inflight_lock.close()
                self._inflight_lock_path(request_key).unlink(missing_ok=True)
            if admitted:
                self._admission_lock.release()
            if reserved_operation is not None:
                self._release_operation_lock(reserved_operation)
            if operation is not None:
                self._whole_call_operation.pop(threading.get_ident(), None)
                self._release_operation_lock(operation)
