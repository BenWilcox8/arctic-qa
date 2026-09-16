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
