"""Response cache: identical requests are served from disk.

The cache key hashes the full model spec (provider, id, params, endpoint
fields) and the rendered messages with media parts content-addressed — so
renaming or moving a media file does not invalidate the cache, but changing
its bytes does.
"""

import contextlib
import hashlib
import json
import os
from dataclasses import asdict, fields
from pathlib import Path

from evaling.config.schema import ModelSpec
from evaling.providers.base import Completion
from evaling.render import RenderedMessage
from evaling.storage import serialize_messages

#: Bumped when the key derivation changes, so old entries are simply missed
#: rather than served against a different request shape.
CACHE_VERSION = 1

#: Model-spec params that never change a response, and so must not change the key.
NON_RESPONSE_PARAMS = frozenset({"pricing", "cost"})


class ResponseCache:
    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)

    def key_for(self, spec: ModelSpec, messages: list[RenderedMessage]) -> str:
        """Hash only what can change the response.

        Deliberately excluded: timeout_s, max_retries, api_key_env, and
        params.pricing. Those are operational knobs — bumping a timeout or
        correcting a price must not throw away every cached response.
        """
        request_params = {
            key: value for key, value in spec.params.items() if key not in NON_RESPONSE_PARAMS
        }
        payload = {
            "v": CACHE_VERSION,
            "provider": spec.provider,
            "model": spec.params.get("model", spec.id),
            "base_url": spec.base_url,
            "command": spec.command,
            "params": request_params,
            "messages": serialize_messages(messages, include_source=False),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
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
        if not isinstance(data, dict):
            return None
        # Drop unknown keys instead of raising: an entry written by a newer
        # evaling must be a miss, not a TypeError that fails the cell.
        known = {f.name for f in fields(Completion)}
        try:
            return Completion(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None

    def put(self, key: str, completion: Completion) -> None:
        """Write an entry. Failures are swallowed: a cache problem must never
        cost the caller a response they already paid for."""
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # temp + rename, so a concurrent reader never sees a partial entry
            tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
            tmp.write_text(json.dumps(asdict(completion), sort_keys=True))
            tmp.replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                path.with_name(f".{path.name}.tmp{os.getpid()}").unlink(missing_ok=True)
