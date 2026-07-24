"""Errors raised by config loading and resolution."""

from evaling.errors import EvalingError


class ConfigError(EvalingError):
    """An eval config or settings source could not be loaded or validated."""
