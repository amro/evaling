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
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

#: Variables that change how output is rendered rather than what it says.
COLOR_VARS = ("FORCE_COLOR", "COLORTERM", "NO_COLOR", "TERM")

#: Where a secrets file would be read from, and every key-shaped variable.
#: Providers name their own (`api_key_env`), so this matches by suffix rather
#: than listing the ones that happen to exist today.
CREDENTIAL_VARS = ("EVALING_SECRETS",)
CREDENTIAL_SUFFIXES = ("_API_KEY", "_API_TOKEN")

#: Injected for the whole session so the stripping has something to strip.
CANARY_VAR = "EVALING_SUITE_CANARY_API_KEY"


class RefusedRequest(BaseException):
    """A test tried to make a real network request.

    ``BaseException`` on purpose. The engine catches ``Exception`` per cell so
    that one provider failure cannot lose a whole run, which would have turned
    this into a recorded cell error and a green run — the accident would be
    prevented but silent, which is the wrong half of the job. Nothing in
    evaling catches ``BaseException``, so it comes out.

    Deliberately not imported by name in the tests that assert on it: mutmut
    runs the suite from a copied tree, where ``conftest`` is a second module
    object whose classes are not identical to these.
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


@pytest.fixture(scope="session", autouse=True)
def _credential_canary():
    """A key-shaped variable that must not survive into any test.

    Without it `test_the_environment_carries_no_credential` passes on any
    machine with nothing exported — which is every CI runner — and so proves
    nothing about the fixture below. This guarantees there is always something
    for it to strip.
    """
    os.environ[CANARY_VAR] = "sk-ant-canary-not-a-real-credential"
    # A private directory for the neutralized user-secrets path. A fixed name
    # in the shared temp directory would be writable by any other user on the
    # machine, who could then plant a file the whole suite would read.
    private = tempfile.mkdtemp(prefix="evaling-suite-")
    yield Path(private) / "no-such-user-secrets.yaml"
    os.environ.pop(CANARY_VAR, None)
    shutil.rmtree(private, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_settings_from_the_environment(monkeypatch):
    """A developer's own EVALING_* variables must not reach the suite.

    These are a settings layer, above a config's `settings:` block, so an
    exported one silently changes what tests run against: EVALING_CONCURRENCY
    reshapes scheduling, and EVALING_OUTPUT_DIR sends a CLI test's runs
    somewhere other than its tmp_path. EVALING_USER_CONFIG was already being
    neutralized per-file, by each test that remembered to.
    """
    for name in list(os.environ):
        if name.startswith("EVALING_") and name != CANARY_VAR:
            monkeypatch.delenv(name, raising=False)
    # Point the user-config layer at nothing, so a contributor's real
    # ~/.config/evaling/config.yaml cannot supply defaults either.
    monkeypatch.setenv("EVALING_USER_CONFIG", "/nonexistent")


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch, _credential_canary):
    """No test may see a real key, whatever the developer has exported.

    The secrets *file* is neutralized too. `build_env` reads
    `~/.config/evaling/secrets.yaml` when a project has no secrets file of its
    own, so a contributor with a real key there would have it loaded by every
    engine-driven test — and a malformed or world-readable one would change
    what unrelated tests see.
    """
    for name in list(os.environ):
        if name in CREDENTIAL_VARS or name.endswith(CREDENTIAL_SUFFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("evaling.secrets.user_secrets_path", lambda: _credential_canary)


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
            # `transport` alone, not `mounts`: a client with mounts but no
            # transport still reaches the network for every unmounted host,
            # and still consults the OS proxy resolver. `transport` composes
            # with `mounts` — it is the fallback for what they do not match.
            if kwargs.get("transport") is None:
                kwargs["transport"] = _refuse()
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(client, "__init__", guarded)
