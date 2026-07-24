"""Config schema, loading, and settings resolution for evaling."""

from evaling.config.cases import load_cases
from evaling.config.errors import ConfigError
from evaling.config.loader import load_config, load_prompt, resolve_prompt
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
    VideoPart,
)
from evaling.config.settings import default_user_config_path, resolve_settings

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
    "VideoPart",
    "default_user_config_path",
    "load_cases",
    "load_config",
    "load_prompt",
    "resolve_prompt",
    "resolve_settings",
]
