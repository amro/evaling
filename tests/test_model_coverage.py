"""The weekly coverage check, exercised without touching a network.

The script's own job is to make network calls, so the parts worth testing are
the ones that decide what counts as a finding: the text-model filter, the
ignore list, and the exit code. Those run on lists handed in directly.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_model_coverage as coverage  # noqa: E402


class TestWhatCountsAsAModelWeShouldPrice:
    @pytest.mark.parametrize(
        "model_id",
        ["gpt-5.2", "claude-opus-5", "gemini-3.8-flash", "o3-mini", "chat-latest"],
    )
    def test_text_models_are_candidates(self, model_id):
        assert coverage.is_text_model(model_id)

    @pytest.mark.parametrize(
        "model_id",
        [
            "text-embedding-3-small",
            "gpt-4o-audio-preview",
            "dall-e-3",
            "whisper-1",
            "lyria-3.5",
            "gemini-2.5-computer-use-preview-10-2025",
            "deep-research-max-preview-04-2026",
        ],
    )
    def test_non_text_models_are_not(self, model_id):
        assert not coverage.is_text_model(model_id)

    @pytest.mark.parametrize(
        "model_id", ["claude-opus-4-5-20251101", "gpt-4o-2024-05-13", "claude-haiku-4-5-20251001"]
    )
    def test_dated_snapshots_are_skipped(self, model_id):
        """A snapshot prices as the base id it pins, so it is not a gap."""
        assert not coverage.is_text_model(model_id)


class TestFindings:
    def test_an_unpriced_text_model_is_reported(self):
        found = coverage.unpriced(["gpt-9-brand-new"], priced={"gpt-5"}, ignored=set())
        assert found == ["gpt-9-brand-new"]

    def test_a_priced_model_is_not(self):
        assert coverage.unpriced(["gpt-5"], priced={"gpt-5"}, ignored=set()) == []

    def test_an_ignored_model_is_not(self):
        found = coverage.unpriced(
            ["gemini-flash-latest"], priced=set(), ignored={"gemini-flash-latest"}
        )
        assert found == []

    def test_the_shipped_ignore_list_parses_and_drops_comments(self):
        ignored = coverage.load_ignored()
        assert "gemini-flash-latest" in ignored
        assert not any(entry.startswith("#") for entry in ignored)
        assert "" not in ignored


class TestItCannotSilentlyPass:
    def test_a_provider_with_no_key_is_reported_not_skipped(self, monkeypatch, capsys):
        """A missing key must not read as a clean sheet — the failure mode of
        every freshness check that measures the wrong thing."""
        for env_var, _ in coverage.PROVIDERS.values():
            monkeypatch.delenv(env_var, raising=False)
        assert coverage.main() == 0
        out = capsys.readouterr().out
        assert "not checked" in out
        assert "nothing missing among the providers that were checked" in out

    def test_it_says_plainly_that_it_does_not_check_rates(self, monkeypatch, capsys):
        for env_var, _ in coverage.PROVIDERS.values():
            monkeypatch.delenv(env_var, raising=False)
        coverage.main()
        assert "cannot tell you a rate is wrong" in capsys.readouterr().out

    def test_a_finding_is_a_non_zero_exit(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setitem(
            coverage.PROVIDERS, "openai", ("OPENAI_API_KEY", lambda key: ["gpt-9-unpriced"])
        )
        assert coverage.main() == 1
        assert "gpt-9-unpriced" in capsys.readouterr().out

    def test_a_provider_that_errors_is_reported_not_a_finding(self, monkeypatch, capsys):
        """A provider being down is not evidence of a coverage gap."""

        def boom(key):
            raise TimeoutError("provider down")

        monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setitem(coverage.PROVIDERS, "openai", ("OPENAI_API_KEY", boom))
        assert coverage.main() == 0
        assert "TimeoutError" in capsys.readouterr().out
