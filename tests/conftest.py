"""Shared test environment.

Two invariants are enforced here rather than left to discipline, because both
have already been broken by tests that looked correct.

The suite asserts on CLI output, and rich decides whether to emit ANSI from
the environment it finds. A developer with `FORCE_COLOR` set — common enough,
and set by some CI images — would see a dozen unrelated failures where
`"6 requests"` is really `"\\x1b[1m6\\x1b[0m requests"`. That is a fact about
their terminal, not about evaling, so it is neutralized once here rather than
worked around in every assertion.

And the suite must never reach the network or use a real credential. That was
a rule in CONTRIBUTING.md and nothing else: a test that ran `evaling run`
against an `openai-compatible` model really did resolve and connect to the
host in its `base_url`, and on a machine with `ANTHROPIC_API_KEY` exported —
evaling's own primary variable, so most contributors' machines — the same
shape of test would have spent that key against the live API. Here it is a
mechanism: keys are removed from the environment, and any HTTP client built
without an injected transport gets one that refuses to send.
"""

import os

import httpx
import pytest

#: Variables that change how output is rendered rather than what it says.
COLOR_VARS = ("FORCE_COLOR", "COLORTERM", "NO_COLOR", "TERM")

#: Where a secrets file would be read from, and every key-shaped variable.
#: Providers name their own (`api_key_env`), so this matches by suffix rather
#: than listing the ones that happen to exist today.
CREDENTIAL_VARS = ("EVALING_SECRETS",)
CREDENTIAL_SUFFIXES = ("_API_KEY", "_API_TOKEN")


class RefusedRequest(RuntimeError):
    """A test tried to make a real network request.

    Deliberately a plain ``RuntimeError`` subclass, and deliberately not
    imported by name in the tests that assert on it: mutmut runs the suite
    from a copied tree, where ``conftest`` is a second module object whose
    classes are not identical to these.
    """


def _refusal(request: httpx.Request) -> str:
    return (
        f"this test tried to reach {request.url} for real. Tests never touch the "
        "network — inject a transport (provider._client = httpx.AsyncClient("
        "transport=httpx.MockTransport(handler))) or use the mock provider."
    )


class _RefuseAsync(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise RefusedRequest(_refusal(request))


class _RefuseSync(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise RefusedRequest(_refusal(request))


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch):
    for name in COLOR_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    """No test may see a real key, whatever the developer has exported."""
    for name in list(os.environ):
        if name in CREDENTIAL_VARS or name.endswith(CREDENTIAL_SUFFIXES):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """A client with no transport injected gets one that refuses to send.

    Refusing at construction would be simpler, but one test legitimately
    builds a client to check that `timeout_s` reached it. Substituting the
    transport lets that keep working while making a *request* the failure —
    and it also stops httpx consulting the OS proxy resolver, which is what
    makes the suite unsafe to run under a `fork()` on macOS.
    """
    for client, refuse in ((httpx.AsyncClient, _RefuseAsync), (httpx.Client, _RefuseSync)):
        original = client.__init__

        def guarded(self, *args, _original=original, _refuse=refuse, **kwargs):
            if kwargs.get("transport") is None and not kwargs.get("mounts"):
                kwargs["transport"] = _refuse()
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(client, "__init__", guarded)
