"""Shared error hierarchy for evaling."""


class EvalingError(Exception):
    """Base class for all evaling errors."""


class TemplateError(EvalingError):
    """A prompt template failed to render."""


class ContentError(EvalingError):
    """A binary content reference could not be resolved."""
