class ArcticQAError(Exception):
    """Base error with a stable public error code."""

    code = "ARCTIC_QA_ERROR"


class DataRootError(ArcticQAError):
    code = "DATA_ROOT_UNAVAILABLE"


class ValidationError(ArcticQAError):
    code = "VALIDATION_ERROR"


class BudgetError(ArcticQAError):
    code = "BUDGET_EXHAUSTED"


class BudgetOverageError(BudgetError):
    code = "BUDGET_OVERAGE"


class ProviderError(ArcticQAError):
    code = "PROVIDER_ERROR"


class AmbiguousChargeError(ProviderError):
    code = "AMBIGUOUS_CHARGE"


class SourceURLError(ArcticQAError):
    code = "SOURCE_URL_NOT_ALLOWED"
