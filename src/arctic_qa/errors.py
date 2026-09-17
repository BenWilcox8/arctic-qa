class ArcticQAError(Exception):
    """Base error with a stable public error code."""

    code = "ARCTIC_QA_ERROR"


class DataRootError(ArcticQAError):
    code = "DATA_ROOT_UNAVAILABLE"


class ValidationError(ArcticQAError):
    code = "VALIDATION_ERROR"


class CandidateRejectedError(ValidationError):
    """A model proposal failed a deterministic construction gate."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class BudgetError(ArcticQAError):
    code = "BUDGET_EXHAUSTED"


class BudgetOverageError(BudgetError):
    code = "BUDGET_OVERAGE"


class PaperCostCapError(BudgetError):
    """One paper family reached its maximum paper cost.

    The cap bounds one family, never the run. The producer records the family,
    skips it and continues; only a whole-run stop ends the producer.
    """

    code = "PAPER_COST_CAP_REACHED"

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage


class BrokerOperationBusyError(ArcticQAError, ValueError):
    """The exclusive broker operation lock stayed held past the bounded wait.

    An ordinary request of the shared broker waits for the lock that a reviewed
    operation holds, because such an operation is short and the request is not
    about it. The wait has a bound, and this error is what the bound raises. It
    is not a money stop and not an authorization refusal: nothing was reserved
    and nothing was submitted, so the producer records it against one family,
    skips that family and continues. It subclasses ``ValueError`` and keeps the
    message the immediate refusal used, so every caller that matches on either
    is unaffected.
    """

    code = "BROKER_OPERATION_BUSY"


class PaperBindingConflictError(ArcticQAError, ValueError):
    """The paper, its family or its source version is already bound elsewhere.

    The ledger binds one family to one paper and one source version, and one
    source version to one family. A request whose identity crosses those
    bindings reserves nothing, submits nothing and is charged nothing, so the
    refusal is about this paper and never about the run. A plain
    ``ValueError`` here ended the sixteen-worker producer at 05:25 UTC on
    2026-09-17 while it had nine papers in flight.
    """


class HarnessUnavailableError(ArcticQAError):
    """A subscription harness binary could not be started for this trial.

    The evaluator runs the Claude Code and Codex arms as child processes of an
    installed binary. A package upgrade replaces that binary, so for a moment
    the path is not there: the Claude Code binary vanished at 12:06 UTC on
    2026-09-17 and again at 12:13, 12:48 and 13:19, and each time the arm went
    dark for the rest of the invocation.

    The probe that raises this runs before the trial reserves its row, so
    nothing is reserved, nothing is submitted and nothing is charged. It proves
    nothing about the model and nothing about the next trial, exactly as
    :class:`BrokerOperationBusyError` does. The evaluator therefore waits a
    bounded time inside the trial and leaves the trial pending; it never
    records a failed response and never stops the arm.
    """

    code = "HARNESS_UNAVAILABLE"


class DuplicateRequestKeyError(ArcticQAError, ValueError):
    """The paid request key is already in the ledger and cannot be reopened.

    A request key names one request exactly, so meeting it again means the
    producer walked back to a call the ledger already holds. The states that
    can be continued are reopened where they are: a resumable ``not_submitted``
    row resumes, a transient ``count_error`` row counts again, and a ``counting``
    row of the same request identity is reused, because the free count reserved
    nothing and submitted nothing. Every other state is a call that was made,
    and replaying it could charge twice, so the request is refused.

    The refusal is about one call of one paper, never about the run: nothing is
    reserved and nothing is submitted by it. The producer records the family,
    skips it and continues. A stop killed the chapter 3 producer at 02:32:10 UTC
    on 2026-09-17 with a ``counting`` row of its own previous start.
    """

    code = "DUPLICATE_REQUEST_KEY"


class CountUnavailableError(ArcticQAError, ValueError):
    """The free countTokens preflight stayed unavailable past its bounded retry.

    ``countTokens`` makes no charge, so a failure of it reserves nothing,
    submits nothing and leaves no money uncertain. A transient provider fault
    there says nothing about the money, the authorization or the request, so it
    is a fault of one paper family and never a run stop: the producer records
    the family, skips it and continues. The counted request keeps its immutable
    count-error receipt, and a later visit of that family counts again under a
    new retry round.

    It subclasses ``ValueError`` for the same reason
    ``BrokerOperationBusyError`` does: ``providers._call_externally_metered``
    turns every unnamed exception into an ``AmbiguousChargeError``, and a free
    call that charged nothing must never be recorded as an unknown charge.
    """

    code = "COUNT_TOKENS_UNAVAILABLE"

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage


class ProviderError(ArcticQAError):
    code = "PROVIDER_ERROR"


class ProviderResponseError(ProviderError):
    """A settled provider response failed its required output contract."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "provider_response_invalid",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class AmbiguousChargeError(ProviderError):
    code = "AMBIGUOUS_CHARGE"


class SourceURLError(ArcticQAError):
    code = "SOURCE_URL_NOT_ALLOWED"


RUN_STOP_ATTRIBUTE = "arctic_qa_run_stop"


def mark_run_stop(error: BaseException) -> BaseException:
    """Mark one exception as a whole-run stop rather than one paper's fault.

    The shared broker is the money and authorization boundary. Everything it
    refuses - the execution gate, the ledger, a ceiling, a halt - describes the
    run, not one candidate, so the producer's candidate-fault containment must
    never swallow it. The exception keeps its class and its message; only the
    marker is added, so every caller that matches on either is unaffected.
    """
    try:
        setattr(error, RUN_STOP_ATTRIBUTE, True)
    except AttributeError:
        # A built-in exception with no instance dictionary cannot carry the
        # marker. The caller's own message rules still apply.
        pass
    return error


def is_run_stop(error: BaseException) -> bool:
    """Say whether this exception was marked a whole-run stop."""
    return bool(getattr(error, RUN_STOP_ATTRIBUTE, False))
