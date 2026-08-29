import pytest

from evaling.cache import ResponseCache
from evaling.config import Case, EvalConfig, Message, ModelSpec
from evaling.engine import run_eval
from evaling.providers.base import Completion
from evaling.render import render_messages
from helpers import make_settings


def rendered(text="hello", tmp_path=None, files=None, content=None):
    message = {"role": "user", "content": content if content is not None else text}
    return render_messages([Message.model_validate(message)], Case(files=files or {}), tmp_path)


def spec(**overrides):
    data = {"id": "m1", "provider": "mock", "params": {"max_tokens": 10}}
    data.update(overrides)
    return ModelSpec.model_validate(data)


def test_roundtrip(tmp_path):
    cache = ResponseCache(tmp_path)
    key = cache.key_for(spec(), rendered(tmp_path=tmp_path))
    assert cache.get(key) is None
    completion = Completion(text="out", input_tokens=2, output_tokens=1, cost_usd=0.0)
    cache.put(key, completion)
    assert cache.get(key) == completion


def test_key_stable_across_instances(tmp_path):
    key_a = ResponseCache(tmp_path).key_for(spec(), rendered(tmp_path=tmp_path))
    key_b = ResponseCache(tmp_path / "other").key_for(spec(), rendered(tmp_path=tmp_path))
    assert key_a == key_b


def test_key_changes_with_prompt(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.key_for(spec(), rendered("a", tmp_path)) != cache.key_for(
        spec(), rendered("b", tmp_path)
    )


def test_media_key_depends_on_content_not_path(tmp_path):
    cache = ResponseCache(tmp_path)
    (tmp_path / "a.png").write_bytes(b"same")
    (tmp_path / "b.png").write_bytes(b"same")
    (tmp_path / "c.png").write_bytes(b"different")
    content = [{"text": "look"}, {"image": "{{ files.img }}"}]

    key_a = cache.key_for(
        spec(), rendered(tmp_path=tmp_path, files={"img": str(tmp_path / "a.png")}, content=content)
    )
    key_b = cache.key_for(
        spec(), rendered(tmp_path=tmp_path, files={"img": str(tmp_path / "b.png")}, content=content)
    )
    key_c = cache.key_for(
        spec(), rendered(tmp_path=tmp_path, files={"img": str(tmp_path / "c.png")}, content=content)
    )
    assert key_a == key_b  # same bytes, different path
    assert key_a != key_c  # different bytes


def test_corrupt_cache_entry_is_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    key = cache.key_for(spec(), rendered(tmp_path=tmp_path))
    cache.put(key, Completion(text="ok"))
    cache._path(key).write_text("{not json", encoding="utf-8")
    assert cache.get(key) is None


def test_entry_with_junk_usage_is_a_miss(tmp_path):
    # Completion validates usage at construction; a tampered or legacy entry
    # carrying junk must read as a miss, not fail the cell.
    cache = ResponseCache(tmp_path)
    key = cache.key_for(spec(), rendered(tmp_path=tmp_path))
    cache.put(key, Completion(text="ok"))
    cache._path(key).write_text('{"text": "ok", "input_tokens": "junk"}', encoding="utf-8")
    assert cache.get(key) is None


def test_entries_sharded_by_key_prefix(tmp_path):
    cache = ResponseCache(tmp_path)
    key = cache.key_for(spec(), rendered(tmp_path=tmp_path))
    cache.put(key, Completion(text="ok"))
    assert (tmp_path / key[:2] / f"{key}.json").is_file()


class TestJudgeCallsAreCachedToo:
    """A judge is a model call, so it belongs in the cache like any other.

    It was not, which made the cache's promise backwards: a rerun of a plain
    eval was free, while a rerun of a *judged* eval — the expensive kind, at
    two or three calls per cell — still paid in full for every judgment.
    """

    def config(self, tmp_path, rubric="Grade 0-1. Answer JSON.", cost=0.01):
        cfg = EvalConfig.model_validate(
            {
                "models": [
                    {"id": "main", "provider": "mock"},
                    {
                        "id": "judge-model",
                        "provider": "mock",
                        "role": "judge",
                        "params": {"response": '{"score": 1, "passed": true}', "cost": cost},
                    },
                ],
                "variants": [{"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
                "cases": [{"id": "c1", "vars": {"q": "alpha"}}],
                "scorecard": [
                    {"criterion": "quality", "scorer": {"type": "llm-judge", "judge": "grader"}}
                ],
                "judges": {
                    "grader": {
                        "model": "judge-model",
                        "rubric": [
                            {"role": "system", "content": rubric},
                            {"role": "user", "content": "Grade: {{ output }}"},
                        ],
                    }
                },
            }
        )
        cfg._base_dir = tmp_path  # noqa: SLF001 - test fixture
        return cfg

    def test_a_rerun_pays_nothing_for_the_same_judgment(self, tmp_path):
        settings = make_settings(tmp_path, cache=True)
        first = run_eval(self.config(tmp_path), settings)
        assert first.totals["judge_cost_usd"] == pytest.approx(0.01)

        second = run_eval(self.config(tmp_path), settings)
        assert second.totals["judge_cost_usd"] == 0.0
        assert second.aggregates["overall"]["score"] == first.aggregates["overall"]["score"]

    def test_a_changed_rubric_is_a_miss(self, tmp_path):
        """Otherwise an edited rubric would be graded by the old one."""
        settings = make_settings(tmp_path, cache=True)
        run_eval(self.config(tmp_path), settings)
        second = run_eval(self.config(tmp_path, rubric="Grade harshly. Answer JSON."), settings)
        assert second.totals["judge_cost_usd"] == pytest.approx(0.01)

    def test_no_cache_still_pays(self, tmp_path):
        settings = make_settings(tmp_path, cache=False)
        run_eval(self.config(tmp_path), settings)
        second = run_eval(self.config(tmp_path), settings)
        assert second.totals["judge_cost_usd"] == pytest.approx(0.01)

    def test_a_credential_in_a_judgment_is_redacted_first(self, tmp_path, monkeypatch):
        """The cache stores the completion, so redacting the record is too late.

        A judge quotes the output it graded; if that output carried a
        credential, the verdict carries it into a file that outlives the run.
        """
        secret = "sk-judge-canary-4417"
        monkeypatch.setenv("JUDGE_KEY", secret)
        config = self.config(tmp_path)
        config.models[1].params["response"] = f'{{"score": 1, "passed": true, "why": "{secret}"}}'
        config.models[1].api_key_env = "JUDGE_KEY"
        settings = make_settings(tmp_path, cache=True)
        run_eval(config, settings)

        cached = list((tmp_path / "cache").rglob("*.json"))
        # Without this the loop is vacuous: if judge responses ever stop being
        # cached, "no file contains the secret" becomes true of no files, and
        # the test goes green while proving the opposite of its name.
        assert cached, "nothing was cached, so nothing here was checked"
        for path in cached:
            assert secret not in path.read_text(encoding="utf-8"), path
