"""evaling: compare prompt variants and models from the command line.

This module is the public, stable API surface — everything the CLI (and the
MCP server) can do is available here programmatically.
"""

__version__ = "0.1.1"

from evaling.config import (  # noqa: E402
    Case,
    ConfigError,
    EvalConfig,
    Settings,
    load_cases,
    load_config,
    resolve_settings,
)
from evaling.engine import (  # noqa: E402
    DryRunReport,
    RunResult,
    config_fingerprint,
    dry_run,
    run_eval,
    run_eval_async,
    select_matrix,
)
from evaling.errors import ContentError, EvalingError, TemplateError  # noqa: E402
from evaling.export import export_run  # noqa: E402
from evaling.privacy import hash_case_id, redact_record  # noqa: E402
from evaling.scorers.base import ScoringError  # noqa: E402
from evaling.scoring import (  # noqa: E402
    GateResult,
    aggregate,
    cell_summary,
    compare_aggregates,
)
from evaling.sources import (  # noqa: E402
    BaseCaseSource,
    CasePage,
    CaseSource,
    SourceError,
)
from evaling.storage import ResultRecord, RunStore, StorageError  # noqa: E402

__all__ = [
    "Case",
    "redact_record",
    "hash_case_id",
    "SourceError",
    "CaseSource",
    "CasePage",
    "BaseCaseSource",
    "ConfigError",
    "ContentError",
    "DryRunReport",
    "EvalConfig",
    "EvalingError",
    "GateResult",
    "ResultRecord",
    "RunResult",
    "RunStore",
    "ScoringError",
    "Settings",
    "StorageError",
    "TemplateError",
    "__version__",
    "aggregate",
    "cell_summary",
    "compare_aggregates",
    "config_fingerprint",
    "dry_run",
    "export_run",
    "load_cases",
    "load_config",
    "resolve_settings",
    "run_eval",
    "run_eval_async",
    "select_matrix",
]
