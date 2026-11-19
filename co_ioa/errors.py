"""Experiment errors, distinct from an infeasible collaboration."""


class ModelServiceError(ValueError):
    """The external model service did not provide a usable response."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class ModelBudgetError(ModelServiceError):
    """A predeclared formal experiment API budget was exhausted."""


class PlanningValidationError(ValueError):
    """The model exhausted its corrections without a valid task proposal."""
