import pytest

from evaling.config import Case
from evaling.errors import EvalingError, TemplateError
from evaling.templating import build_context, render_text


def test_renders_variables():
    assert render_text("Hello {{ name }}!", {"name": "world"}) == "Hello world!"


def test_supports_conditionals_and_loops():
    template = "{% for x in items %}{{ x }}{% if not loop.last %}, {% endif %}{% endfor %}"
    assert render_text(template, {"items": ["a", "b", "c"]}) == "a, b, c"


def test_undefined_variable_raises_template_error():
    with pytest.raises(TemplateError, match="'name' is undefined"):
        render_text("Hello {{ name }}!", {})


def test_undefined_attribute_raises_template_error():
    with pytest.raises(TemplateError, match="photo"):
        render_text("{{ files.photo }}", {"files": {}})


def test_syntax_error_raises_template_error():
    with pytest.raises(TemplateError, match="syntax error"):
        render_text("{% if x %}unclosed", {"x": 1})


def test_error_message_includes_location():
    with pytest.raises(TemplateError, match=r"message 2 \(user\):"):
        render_text("{{ nope }}", {}, where="message 2 (user)")


def test_trailing_newline_preserved():
    assert render_text("line\n", {}) == "line\n"


def test_build_context_promotes_vars_and_files():
    case = Case(vars={"q": "hi"}, files={"photo": "/abs/dog.jpg"})
    context = build_context(case)
    assert context["q"] == "hi"
    assert context["files"]["photo"] == "/abs/dog.jpg"


def test_build_context_rejects_reserved_var_names():
    case = Case(id="c1", vars={"files": "clash"})
    with pytest.raises(TemplateError, match="c1.*reserved variable"):
        build_context(case)


def test_template_error_is_evaling_error():
    assert issubclass(TemplateError, EvalingError)


def test_config_error_is_evaling_error():
    from evaling.config import ConfigError

    assert issubclass(ConfigError, EvalingError)
