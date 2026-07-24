import pytest

from evaling.config import Case, Message
from evaling.content import MediaRef
from evaling.errors import ContentError, TemplateError
from evaling.render import RenderedText, render_messages


def messages_from(*dicts):
    return [Message.model_validate(d) for d in dicts]


def test_renders_string_content_as_single_text_part(tmp_path):
    messages = messages_from({"role": "user", "content": "Q: {{ q }}"})
    case = Case(vars={"q": "why?"})
    [message] = render_messages(messages, case, tmp_path)
    assert message.role == "user"
    assert message.parts == (RenderedText("Q: why?"),)
    assert message.text == "Q: why?"


def test_renders_multi_turn_conversation(tmp_path):
    messages = messages_from(
        {"role": "system", "content": "Be {{ tone }}."},
        {"role": "user", "content": "{{ q }}"},
        {"role": "assistant", "content": "Draft: {{ q }}"},
    )
    case = Case(vars={"tone": "brief", "q": "hi"})
    rendered = render_messages(messages, case, tmp_path)
    assert [m.role for m in rendered] == ["system", "user", "assistant"]
    assert rendered[0].text == "Be brief."
    assert rendered[2].text == "Draft: hi"


def test_renders_media_part_via_files_reference(tmp_path):
    (tmp_path / "dog.jpg").write_bytes(b"jpeg bytes")
    messages = messages_from(
        {
            "role": "user",
            "content": [{"text": "{{ q }}"}, {"image": "{{ files.photo }}"}],
        }
    )
    case = Case(vars={"q": "breed?"}, files={"photo": str(tmp_path / "dog.jpg")})
    [message] = render_messages(messages, case, tmp_path)
    text, media = message.parts
    assert text == RenderedText("breed?")
    assert isinstance(media, MediaRef)
    assert media.kind == "image"
    assert media.media_type == "image/jpeg"


def test_renders_literal_media_path_relative_to_base_dir(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF")
    messages = messages_from({"role": "user", "content": [{"file": "doc.pdf"}]})
    [message] = render_messages(messages, Case(), tmp_path)
    assert message.parts[0].media_type == "application/pdf"


def test_audio_part_renders(tmp_path):
    (tmp_path / "clip.mp3").write_bytes(b"mp3")
    messages = messages_from({"role": "user", "content": [{"audio": "clip.mp3"}]})
    [message] = render_messages(messages, Case(), tmp_path)
    assert message.parts[0].kind == "audio"


def test_undefined_var_error_names_message(tmp_path):
    messages = messages_from(
        {"role": "system", "content": "ok"},
        {"role": "user", "content": "{{ missing }}"},
    )
    with pytest.raises(TemplateError, match=r"message 2 \(user\).*'missing' is undefined"):
        render_messages(messages, Case(), tmp_path)


def test_missing_media_error_names_message(tmp_path):
    messages = messages_from({"role": "user", "content": [{"image": "ghost.png"}]})
    with pytest.raises(ContentError, match=r"message 1 \(user\).*not found"):
        render_messages(messages, Case(), tmp_path)


def test_text_property_joins_only_text_parts(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    messages = messages_from(
        {
            "role": "user",
            "content": [{"text": "before "}, {"image": "a.png"}, {"text": "after"}],
        }
    )
    [message] = render_messages(messages, Case(), tmp_path)
    assert message.text == "before after"
