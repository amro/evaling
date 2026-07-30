"""A malformed config must produce a message, never a traceback.

The config is strict, user-authored, and the first thing anyone touches, so a
typo in it is the most common failure evaling has. The contract is narrow:
every entry point either loads the file or raises ConfigError with something
worth reading. Anything else — a Pydantic dump, a YAML internal, a bare
KeyError, a RecursionError — is a bug, because the user cannot act on it and
it looks like evaling broke rather than their file being wrong.

Two axes, both deterministic so a failure reproduces from its seed alone:
structural mutation of a valid config, and byte-level mutation of its text.
Hand-written cases cover the shapes random mutation will not reach — a YAML
bomb, a wrong text encoding, a tag that would execute something.
"""

import copy
import random

import pytest
import yaml

from evaling.config import load_config
from evaling.config.errors import ConfigError
from evaling.config.loader import load_project_settings, load_prompt
from evaling.config.settings import resolve_settings
from evaling.secrets import SecretsError, load_secrets

VALID = {
    "settings": {"concurrency": 4, "cache": True, "output_dir": "runs"},
    "models": [
        {"id": "m1", "provider": "mock", "params": {"pricing": {"input": 1.0, "output": 2.0}}},
        {"id": "judge-m", "provider": "mock", "role": "judge"},
    ],
    "variants": [
        {"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]},
        {"name": "v2", "prompt": "prompts/other.yaml"},
    ],
    "cases": [
        {"id": "c1", "vars": {"q": "alpha"}, "expected": "alpha"},
        {"id": "c2", "vars": {"q": "beta"}, "human_label": "good"},
    ],
    "scorecard": [
        {"criterion": "acc", "scorer": {"type": "exact"}},
        {"criterion": "graded", "weight": 2, "scorer": {"type": "llm-judge", "judge": "j"}},
    ],
    "judges": {"j": {"model": "judge-m", "rubric": [{"role": "user", "content": "grade"}]}},
    "thresholds": {"min_pass_rate": 0.5, "baseline": "regression"},
    "privacy": {"no_look": False},
}

#: Values chosen to break assumptions rather than to be plausible: wrong types,
#: boundary numbers, empty containers, and text that means something to a
#: layer below (a template opener, a YAML tag, a NUL).
HOSTILE = [
    None,
    True,
    False,
    0,
    -1,
    1.5,
    "",
    "   ",
    "x" * 4096,
    [],
    {},
    [None],
    [[[["deep"]]]],
    {"": None},
    {"unexpected": {"nested": [1, 2, 3]}},
    10**40,
    -(10**40),
    "{{ unclosed",
    "!!python/object",
    "\U0001f600",
    "café",
    "-",
    "0",
    "null",
    "regression",
]


def paths(data, prefix=()):
    """Every addressable location in a nested structure."""
    found = [prefix] if prefix else []
    if isinstance(data, dict):
        for key, value in data.items():
            found.extend(paths(value, (*prefix, key)))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            found.extend(paths(value, (*prefix, index)))
    return found


def at(data, path):
    for step in path:
        data = data[step]
    return data


def mutate(rng, data):
    """One random edit: replace a value, delete a key, or add an unknown one."""
    candidates = paths(data)
    if not candidates:
        return data
    path = rng.choice(candidates)
    parent, key = at(data, path[:-1]), path[-1]
    operation = rng.choice(("replace", "delete", "insert"))
    if operation == "replace":
        parent[key] = rng.choice(HOSTILE)
    elif operation == "delete":
        del parent[key]
    elif isinstance(parent[key], dict):
        parent[key][f"unknown{rng.randrange(100)}"] = rng.choice(HOSTILE)
    else:
        parent[key] = rng.choice(HOSTILE)
    return data


def write(tmp_path, text):
    path = tmp_path / "eval.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def check(path):
    """Load it. Success is fine; ConfigError is fine; anything else is the bug."""
    try:
        load_config(path)
    except ConfigError as exc:
        assert str(exc).strip(), "ConfigError carried no message"


class TestStructuralMutation:
    """Well-formed YAML that is the wrong shape."""

    @pytest.mark.parametrize("seed", range(400))
    def test_one_edit_never_escapes_as_a_traceback(self, seed, tmp_path):
        rng = random.Random(seed)
        data = mutate(rng, copy.deepcopy(VALID))
        try:
            text = yaml.safe_dump(data, allow_unicode=True)
        except yaml.YAMLError:
            pytest.skip("mutation is not representable as YAML")
        check(write(tmp_path, text))

    @pytest.mark.parametrize("seed", range(200))
    def test_several_edits_never_escape_either(self, seed, tmp_path):
        """Single edits mostly hit one validator; stacked ones reach the rest."""
        rng = random.Random(10_000 + seed)
        data = copy.deepcopy(VALID)
        for _ in range(rng.randrange(2, 6)):
            data = mutate(rng, data)
            if not isinstance(data, dict):
                break
        try:
            text = yaml.safe_dump(data, allow_unicode=True)
        except yaml.YAMLError:
            pytest.skip("mutation is not representable as YAML")
        check(write(tmp_path, text))


class TestTextMutation:
    """Text that may not be YAML at all — the half a schema never sees."""

    BASE = yaml.safe_dump(VALID, allow_unicode=True)

    def damage(self, rng, text):
        lines = text.splitlines(keepends=True)
        operation = rng.choice(("drop", "duplicate", "tab", "insert", "truncate", "unindent"))
        index = rng.randrange(len(lines))
        if operation == "drop":
            del lines[index]
        elif operation == "duplicate":
            lines.insert(index, lines[index])
        elif operation == "tab":
            lines[index] = lines[index].replace("  ", "\t", 1)
        elif operation == "insert":
            char = rng.choice("[]{}:,-\"'#&*!|>%@`\\")
            position = rng.randrange(len(lines[index]) + 1)
            lines[index] = lines[index][:position] + char + lines[index][position:]
        elif operation == "truncate":
            lines[index] = lines[index][: rng.randrange(len(lines[index]) + 1)]
        else:
            lines[index] = lines[index].lstrip()
        return "".join(lines)

    @pytest.mark.parametrize("seed", range(400))
    def test_damaged_text_never_escapes_as_a_traceback(self, seed, tmp_path):
        rng = random.Random(20_000 + seed)
        text = self.BASE
        for _ in range(rng.randrange(1, 4)):
            text = self.damage(rng, text)
        check(write(tmp_path, text))


class TestHandPickedHostileFiles:
    """Shapes random mutation will not stumble into."""

    def test_an_empty_file_says_so(self, tmp_path):
        with pytest.raises(ConfigError, match="mapping"):
            load_config(write(tmp_path, ""))

    def test_a_top_level_list_says_so(self, tmp_path):
        with pytest.raises(ConfigError, match="mapping"):
            load_config(write(tmp_path, "- a\n- b\n"))

    def test_a_python_object_tag_is_refused(self, tmp_path):
        """safe_load must stay safe_load: this would otherwise run a command."""
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config(write(tmp_path, "models: !!python/object/apply:os.system ['echo hi']\n"))

    def test_two_documents_in_one_file(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(write(tmp_path, "models: []\n---\nvariants: []\n"))

    def test_duplicate_keys_do_not_crash(self, tmp_path):
        check(write(tmp_path, "models: []\nmodels: []\ncases: []\n"))

    def test_a_nul_byte(self, tmp_path):
        check(write(tmp_path, "models: \x00\n"))

    def test_a_byte_order_mark(self, tmp_path):
        check(write(tmp_path, "﻿models: []\n"))

    def test_deeply_nested_structure(self, tmp_path):
        """PyYAML recurses while composing, and RecursionError is not a YAMLError."""
        depth = 5_000
        check(write(tmp_path, "cases: " + "[" * depth + "]" * depth + "\n"))

    def test_deeply_nested_block_structure(self, tmp_path):
        depth = 5_000
        check(write(tmp_path, "cases:\n" + "".join("  " * i + "- \n" for i in range(depth))))

    def test_an_alias_bomb_stays_bounded(self, tmp_path):
        """Modest expansion — enough to prove aliases are handled, not a DoS."""
        text = "a: &a [x, x, x, x]\nb: &b [*a, *a, *a, *a]\nc: [*b, *b, *b, *b]\nmodels: []\n"
        check(write(tmp_path, text))

    def test_a_file_that_is_not_utf8(self, tmp_path):
        """A config saved as latin-1 or UTF-16 is a mistake, not a crash."""
        path = tmp_path / "eval.yaml"
        path.write_bytes("models: [{id: café, provider: mock}]\n".encode("latin-1"))
        with pytest.raises(ConfigError):
            load_config(path)

    def test_a_utf16_file(self, tmp_path):
        path = tmp_path / "eval.yaml"
        path.write_bytes("models: []\n".encode("utf-16"))
        with pytest.raises(ConfigError):
            load_config(path)

    def test_a_directory_where_a_config_should_be(self, tmp_path):
        (tmp_path / "eval.yaml").mkdir()
        with pytest.raises(ConfigError):
            load_config(tmp_path / "eval.yaml")


class TestTheOtherLoaders:
    """Every file evaling reads gets the same contract, not just eval.yaml."""

    @pytest.mark.parametrize("seed", range(120))
    def test_project_settings_never_traceback(self, seed, tmp_path):
        rng = random.Random(30_000 + seed)
        data = mutate(rng, copy.deepcopy(VALID))
        try:
            text = yaml.safe_dump(data, allow_unicode=True)
        except yaml.YAMLError:
            pytest.skip("mutation is not representable as YAML")
        path = write(tmp_path, text)
        try:
            load_project_settings(path)
        except ConfigError as exc:
            assert str(exc).strip()

    def test_project_settings_on_a_non_utf8_file(self, tmp_path):
        path = tmp_path / "eval.yaml"
        path.write_bytes("settings: {output_dir: café}\n".encode("latin-1"))
        with pytest.raises(ConfigError):
            load_project_settings(path)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "- not-a-mapping",
            "- role: nonsense\n  content: hi\n",
            "- {role: user}\n",
            "role: user\ncontent: hi\n",
            "- " + "[" * 3000 + "]" * 3000 + "\n",
            "!!python/object/apply:os.system ['echo hi']\n",
        ],
    )
    def test_prompt_files_never_traceback(self, text, tmp_path):
        path = tmp_path / "prompt.yaml"
        path.write_text(text, encoding="utf-8", newline="\n")
        with pytest.raises(ConfigError) as caught:
            load_prompt(path)
        assert str(caught.value).strip()

    def test_a_prompt_file_that_is_not_utf8(self, tmp_path):
        path = tmp_path / "prompt.yaml"
        path.write_bytes("- {role: user, content: café}\n".encode("latin-1"))
        with pytest.raises(ConfigError):
            load_prompt(path)

    @pytest.mark.parametrize(
        "text",
        ["", "- a\n- b\n", "KEY: {nested: value}\n", "KEY: [1, 2]\n", "1: 2\n", "\x00\n"],
    )
    def test_secrets_files_never_traceback(self, text, tmp_path):
        path = tmp_path / ".evaling.secrets.yaml"
        path.write_text(text, encoding="utf-8", newline="\n")
        try:
            load_secrets(tmp_path, env={})
        except SecretsError as exc:
            assert str(exc).strip()

    def test_a_secrets_file_that_is_not_utf8(self, tmp_path):
        path = tmp_path / ".evaling.secrets.yaml"
        path.write_bytes("KEY: café\n".encode("latin-1"))
        with pytest.raises(SecretsError):
            load_secrets(tmp_path, env={})

    @pytest.mark.parametrize(
        ("name", "text"),
        [
            ("cases.jsonl", ""),
            ("cases.jsonl", "not json\n"),
            ("cases.jsonl", "[1, 2]\n"),
            ("cases.jsonl", '{"files": "not-a-mapping"}\n'),
            ("cases.jsonl", '{"vars": 3}\n'),
            ("cases.jsonl", "\x00\n"),
            ("cases.csv", ""),
            ("cases.csv", "id,q\n"),
            ("cases.csv", "id,q\n1\n"),
            ("cases.csv", "id,q\n1,2,3,4\n"),
            ("cases.csv", 'id,q\n1,"unterminated\n'),
            ("cases.csv", "id,q\n\x001,2\n"),
            ("cases.txt", "whatever\n"),
        ],
    )
    def test_case_files_never_traceback(self, name, text, tmp_path):
        """A dataset is as hand-authored as the config, and read the same way."""
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
        config = write(
            tmp_path,
            "models: [{id: m, provider: mock}]\n"
            'variants: [{name: v, prompt: [{role: user, content: "hi"}]}]\n'
            f"cases: {{file: {name}}}\n"
            "scorecard: [{criterion: c, scorer: {type: exact}}]\n",
        )
        from evaling.config.cases import load_cases

        try:
            load_cases(load_config(config))
        except ConfigError as exc:
            assert str(exc).strip()

    @pytest.mark.parametrize("name", ["cases.jsonl", "cases.csv"])
    def test_a_case_file_that_is_not_utf8(self, name, tmp_path):
        (tmp_path / name).write_bytes(
            '{"id": "1", "q": "café"}\n'.encode("latin-1")
            if name.endswith("jsonl")
            else "id,q\n1,café\n".encode("latin-1")
        )
        config = write(
            tmp_path,
            "models: [{id: m, provider: mock}]\n"
            'variants: [{name: v, prompt: [{role: user, content: "hi"}]}]\n'
            f"cases: {{file: {name}}}\n"
            "scorecard: [{criterion: c, scorer: {type: exact}}]\n",
        )
        from evaling.config.cases import load_cases

        with pytest.raises(ConfigError, match="UTF-8"):
            load_cases(load_config(config))

    @pytest.mark.parametrize(
        "value", ["", "  ", "nonsense", "-1", "0", "1e400", "𝟛", "true-ish", "9" * 400]
    )
    def test_env_overrides_never_traceback(self, value, tmp_path):
        env = {
            "EVALING_CONCURRENCY": value,
            "EVALING_CACHE": value,
            "EVALING_OUTPUT_DIR": value,
            "EVALING_USER_CONFIG": str(tmp_path / "missing.yaml"),
        }
        try:
            resolve_settings(None, None, env=env, user_config_path=tmp_path / "missing.yaml")
        except ConfigError as exc:
            assert str(exc).strip()

    @pytest.mark.parametrize(
        "text", ["- a\n", "concurrency: nope\n", "output_dir: {}\n", "unknown: 1\n", "\x00\n"]
    )
    def test_user_config_never_tracebacks(self, text, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8", newline="\n")
        try:
            resolve_settings(None, None, env={}, user_config_path=path)
        except ConfigError as exc:
            assert str(exc).strip()

    def test_a_user_config_that_is_not_utf8(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_bytes("output_dir: café\n".encode("latin-1"))
        with pytest.raises(ConfigError):
            resolve_settings(None, None, env={}, user_config_path=path)
