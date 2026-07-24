"""End-to-end runs of the committed fixture evals in tests/fixtures/e2e/.

Each scenario is a complete eval project (config, prompts, datasets, media)
executed through the real engine against the mock provider — no mocked
internals, no network.
"""

import json
from pathlib import Path

import pytest

from evaling import run_eval
from evaling.config import Settings, load_config
from evaling.storage import RunStore

E2E = Path(__file__).parent / "fixtures" / "e2e"

SCENARIOS = {
    "text-single": 8,  # 2 variants x 2 models x 2 cases
    "text-multi": 3,  # 1 x 1 x 3
    "media-single": 2,  # 1 x 1 x 2
    "media-multi": 2,  # 1 x 1 x 2
}


def run_scenario(name, tmp_path, **settings_overrides):
    config = load_config(E2E / name / "eval.yaml")
    settings = Settings.model_validate(
        {
            "output_dir": str(tmp_path / "runs"),
            "cache_dir": str(tmp_path / "cache"),
            "cache": settings_overrides.pop("cache", False),
            **settings_overrides,
        }
    )
    return run_eval(config, settings), settings


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_completes_cleanly(name, tmp_path):
    result, settings = run_scenario(name, tmp_path)
    assert result.counts["total"] == SCENARIOS[name]
    assert result.counts["failed"] == 0
    assert all(r.output for r in result.records)

    # everything persisted and loadable
    store = RunStore(settings.output_dir)
    assert len(store.load_results(result.run_id)) == SCENARIOS[name]
    meta = json.loads((result.path / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "complete"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_second_run_fully_cached(name, tmp_path):
    first, settings = run_scenario(name, tmp_path, cache=True)
    assert first.counts["cached"] == 0
    second = run_eval(load_config(E2E / name / "eval.yaml"), settings)
    assert second.counts["cached"] == SCENARIOS[name]
    assert {r.output for r in second.records} == {r.output for r in first.records}


def test_text_single_outputs(tmp_path):
    result, _ = run_scenario("text-single", tmp_path)
    by_key = {r.key: r for r in result.records}
    assert by_key[("plain", "mock-echo", "greeting")].output == "hello?"
    assert by_key[("instructed", "mock-echo", "math")].output == "Q: what is 2+2?"
    assert by_key[("plain", "mock-fixed", "greeting")].output == "FIXED ANSWER"


def test_text_multi_echoes_final_turn(tmp_path):
    # The mock echoes the last user message, proving the full conversation
    # (system + history + follow-up) rendered per case.
    result, _ = run_scenario("text-multi", tmp_path)
    by_case = {r.case_id: r for r in result.records}
    assert by_case["trip"].output == "Somewhere warm in December."
    assert by_case["menu"].output == "Something vegetarian with mushrooms."
    # all four turns present in the stored messages
    roles = [m["role"] for m in by_case["code"].messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert by_case["code"].messages[0]["parts"][0]["text"] == "You are a programmer."


def test_media_single_all_kinds_flow_through(tmp_path):
    result, _ = run_scenario("media-single", tmp_path)
    record = next(r for r in result.records if r.case_id == "all-media")
    # mock echo appends a content-hash marker per media part
    for kind in ("image", "file", "audio", "video"):
        assert f"[{kind}:" in record.output
    part_types = [p["type"] for p in record.messages[0]["parts"]]
    assert part_types == ["text", "image", "file", "audio", "video"]

    # four unique files -> four content-addressed artifacts (shared across cases)
    artifacts = list((result.path / "artifacts").iterdir())
    assert len(artifacts) == 4
    suffixes = {a.suffix for a in artifacts}
    assert suffixes == {".png", ".pdf", ".mp3", ".mp4"}


def test_media_multi_turns_and_artifact_dedup(tmp_path):
    result, _ = run_scenario("media-multi", tmp_path)
    record = result.records[0]
    roles = [m["role"] for m in record.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert record.messages[1]["parts"][1]["type"] == "image"
    assert record.messages[3]["parts"][1]["type"] == "file"
    # the mock echoes the LAST user turn: text + the pdf marker
    assert record.output.startswith("Now answer using this document:")
    assert "[file:" in record.output

    # both cases share the same two files -> exactly two artifacts
    artifacts = list((result.path / "artifacts").iterdir())
    assert len(artifacts) == 2
