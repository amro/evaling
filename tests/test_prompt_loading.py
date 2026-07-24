import pytest

from evaling.config import ConfigError, Message
from evaling.config.loader import load_prompt, resolve_prompt


def test_loads_yaml_message_list(tmp_path):
    prompt = tmp_path / "p.yaml"
    prompt.write_text(
        """
- role: system
  content: Be brief.
- role: user
  content:
    - text: "{{ q }}"
    - image: "{{ files.photo }}"
"""
    )
    messages = load_prompt(prompt)
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[1].content[1].image == "{{ files.photo }}"


def test_missing_prompt_file(tmp_path):
    with pytest.raises(ConfigError, match="prompt file not found"):
        load_prompt(tmp_path / "ghost.yaml")


def test_non_list_prompt_rejected(tmp_path):
    prompt = tmp_path / "p.yaml"
    prompt.write_text("role: user\ncontent: hi\n")
    with pytest.raises(ConfigError, match="must be a YAML list of messages, got dict"):
        load_prompt(prompt)


def test_empty_prompt_rejected(tmp_path):
    prompt = tmp_path / "p.yaml"
    prompt.write_text("[]\n")
    with pytest.raises(ConfigError, match="contains no messages"):
        load_prompt(prompt)


def test_invalid_message_reports_position(tmp_path):
    prompt = tmp_path / "p.yaml"
    prompt.write_text("- role: wizard\n  content: hi\n")
    with pytest.raises(ConfigError, match=r"p\.yaml: message 1: role"):
        load_prompt(prompt)


def test_resolve_prompt_passes_through_inline_messages(tmp_path):
    inline = [Message(role="user", content="hi")]
    assert resolve_prompt(inline, tmp_path) is inline


def test_resolve_prompt_loads_path_relative_to_base_dir(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "v1.yaml").write_text("- role: user\n  content: hi\n")
    messages = resolve_prompt("prompts/v1.yaml", tmp_path)
    assert messages[0].content == "hi"
