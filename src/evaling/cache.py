"""Response cache: identical requests are served from disk.

The cache key hashes the full model spec (provider, id, params, endpoint
fields) and the rendered messages with media parts content-addressed — so
renaming or moving a media file does not invalidate the cache, but changing
its bytes does.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from evaling.config.schema import ModelSpec
from evaling.providers.base import Completion
from evaling.render import RenderedMessage
from evaling.storage import serialize_messages


class ResponseCache:
    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)

    def key_for(self, spec: ModelSpec, messages: list[RenderedMessage]) -> str:
        payload = {
            "model": spec.model_dump(mode="json"),
            "messages": serialize_messages(messages, include_source=False),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> Completion | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None  # unreadable cache entries are misses, never fatal
        return Completion(**data)

    def put(self, key: str, completion: Completion) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(completion), sort_keys=True))
