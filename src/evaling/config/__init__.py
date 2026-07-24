"""Config schema, loading, and settings resolution for evaling."""

from evaling.config.errors import ConfigError
from evaling.config.loader import load_config
from evaling.config.schema import (
    AudioPart,
    Case,
    CaseFileRef,
    ContentPart,
    CriterionSpec,
    EvalConfig,
    FilePart,
    ImagePart,
    JudgeSpec,
    Message,
    ModelSpec,
    ScorerSpec,
    Settings,
    TextPart,
    Thresholds,
    VariantSpec,
)

__all__ = [
    "AudioPart",
    "Case",
    "CaseFileRef",
    "ConfigError",
    "ContentPart",
    "CriterionSpec",
    "EvalConfig",
    "FilePart",
    "ImagePart",
    "JudgeSpec",
    "Message",
    "ModelSpec",
    "ScorerSpec",
    "Settings",
    "TextPart",
    "Thresholds",
    "VariantSpec",
    "load_config",
]
