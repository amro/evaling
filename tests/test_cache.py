from evaling.cache import ResponseCache
from evaling.config import Case, Message, ModelSpec
from evaling.providers.base import Completion
from evaling.render import render_messages


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


def test_key_changes_with_params(tmp_path):
    cache = ResponseCache(tmp_path)
    messages = rendered(tmp_path=tmp_path)
    assert cache.key_for(spec(), messages) != cache.key_for(
        spec(params={"max_tokens": 99}), messages
    )


def test_key_changes_with_model_id(tmp_path):
    cache = ResponseCache(tmp_path)
    messages = rendered(tmp_path=tmp_path)
    assert cache.key_for(spec(), messages) != cache.key_for(spec(id="m2"), messages)


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


def test_entries_sharded_by_key_prefix(tmp_path):
    cache = ResponseCache(tmp_path)
    key = cache.key_for(spec(), rendered(tmp_path=tmp_path))
    cache.put(key, Completion(text="ok"))
    assert (tmp_path / key[:2] / f"{key}.json").is_file()
