import json

import pytest

from evaling.config import ConfigError, EvalConfig
from evaling.config.cases import load_cases


def config_with(cases, base_dir=None, tmp_path=None):
    cfg = EvalConfig.model_validate(
        {
            "models": [{"id": "m1", "provider": "mock"}],
            "variants": [{"name": "v1", "prompt": "p.yaml"}],
            "cases": cases,
            "scorecard": [{"criterion": "acc", "scorer": {"type": "exact"}}],
        }
    )
    if base_dir is not None:
        cfg._base_dir = base_dir
    elif tmp_path is not None:
        cfg._base_dir = tmp_path
    return cfg


def test_inline_cases_get_generated_ids(tmp_path):
    cases = load_cases(config_with([{"vars": {"q": "a"}}, {"vars": {"q": "b"}}], tmp_path))
    assert [c.id for c in cases] == ["case-1", "case-2"]


def test_inline_explicit_ids_kept(tmp_path):
    cases = load_cases(config_with([{"id": "alpha"}, {}], tmp_path))
    assert [c.id for c in cases] == ["alpha", "case-2"]


def test_generated_id_collision_with_explicit_id_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate case id: 'case-2'"):
        load_cases(config_with([{"id": "case-2"}, {}], tmp_path))


def test_inline_relative_files_resolve_against_config_dir(tmp_path):
    cfg = config_with([{"files": {"photo": "fixtures/dog.jpg"}}], tmp_path)
    [case] = load_cases(cfg)
    assert case.files["photo"] == str((tmp_path / "fixtures/dog.jpg").resolve())


def test_jsonl_rows_split_reserved_fields_and_vars(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "c1",
                "question": "breed?",
                "expected": "collie",
                "human_label": 5,
                "photo": "file://images/dog.jpg",
            }
        )
        + "\n"
    )
    [case] = load_cases(config_with({"file": "cases.jsonl"}, tmp_path))
    assert case.id == "c1"
    assert case.vars == {"question": "breed?"}
    assert case.expected == "collie"
    assert case.human_label == 5
    assert case.files["photo"] == str((tmp_path / "images/dog.jpg").resolve())


def test_jsonl_files_mapping_supported(tmp_path):
    dataset = tmp_path / "data" / "cases.jsonl"
    dataset.parent.mkdir()
    dataset.write_text(json.dumps({"files": {"doc": "doc.pdf"}}) + "\n")
    [case] = load_cases(config_with({"file": "data/cases.jsonl"}, tmp_path))
    # dataset-relative resolution: against data/, not the config dir
    assert case.files["doc"] == str((tmp_path / "data" / "doc.pdf").resolve())


def test_jsonl_blank_lines_skipped(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text('{"q": "a"}\n\n{"q": "b"}\n')
    assert len(load_cases(config_with({"file": "cases.jsonl"}, tmp_path))) == 2


def test_jsonl_invalid_json_reports_line(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text('{"q": "a"}\n{oops\n')
    with pytest.raises(ConfigError, match=r"cases\.jsonl:2: invalid JSON"):
        load_cases(config_with({"file": "cases.jsonl"}, tmp_path))


def test_jsonl_non_object_line_rejected(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("[1, 2]\n")
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_cases(config_with({"file": "cases.jsonl"}, tmp_path))


def test_csv_rows_load_with_reserved_columns(tmp_path):
    dataset = tmp_path / "cases.csv"
    dataset.write_text(
        "id,question,expected,photo\nc1,breed?,collie,file://dog.jpg\n,color?,,file://cat.jpg\n"
    )
    cases = load_cases(config_with({"file": "cases.csv"}, tmp_path))
    assert cases[0].id == "c1"
    assert cases[0].vars == {"question": "breed?"}
    assert cases[0].files["photo"] == str((tmp_path / "dog.jpg").resolve())
    # empty reserved cells mean "not provided"
    assert cases[1].id == "case-2"
    assert cases[1].expected is None


def test_missing_case_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="case file not found"):
        load_cases(config_with({"file": "ghost.jsonl"}, tmp_path))


def test_empty_case_file_raises(tmp_path):
    (tmp_path / "cases.jsonl").write_text("")
    with pytest.raises(ConfigError, match="case file is empty"):
        load_cases(config_with({"file": "cases.jsonl"}, tmp_path))


def test_unsupported_case_file_extension_raises(tmp_path):
    (tmp_path / "cases.xlsx").write_bytes(b"x")
    with pytest.raises(ConfigError, match=r"unsupported case file type '\.xlsx'"):
        load_cases(config_with({"file": "cases.xlsx"}, tmp_path))


def test_duplicate_ids_across_dataset_rejected(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text('{"id": "dup"}\n{"id": "dup"}\n')
    with pytest.raises(ConfigError, match="duplicate case id: 'dup'"):
        load_cases(config_with({"file": "cases.jsonl"}, tmp_path))
