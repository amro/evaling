"""Jinja2 rendering for prompt text.

Templates render with strict undefined: referencing a variable that a case does
not define is an error, so typos fail loudly instead of producing empty text.
"""

from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError
from jinja2.exceptions import UndefinedError

from evaling.config.schema import Case
from evaling.errors import TemplateError

_env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)

# Names injected into every template context; cases may not define vars with
# these names.
RESERVED_VAR_NAMES = frozenset({"files"})


def render_text(template: str, context: dict[str, Any], where: str = "template") -> str:
    """Render one Jinja2 template string, raising TemplateError on failure."""
    try:
        return _env.from_string(template).render(context)
    except TemplateSyntaxError as exc:
        raise TemplateError(f"{where}: template syntax error: {exc.message}") from exc
    except UndefinedError as exc:
        raise TemplateError(f"{where}: {exc.message}") from exc


def build_context(case: Case) -> dict[str, Any]:
    """Build the template context for a case.

    Case vars are exposed as top-level names; file attachments are exposed as
    ``files.<name>``.
    """
    reserved = RESERVED_VAR_NAMES & case.vars.keys()
    if reserved:
        names = ", ".join(sorted(repr(name) for name in reserved))
        raise TemplateError(f"case {case.id or '<unnamed>'}: reserved variable name(s): {names}")
    return {**case.vars, "files": dict(case.files)}
