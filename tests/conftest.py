"""Shared test environment.

The suite asserts on CLI output, and rich decides whether to emit ANSI from
the environment it finds. A developer with `FORCE_COLOR` set — common enough,
and set by some CI images — would see a dozen unrelated failures where
`"6 requests"` is really `"\\x1b[1m6\\x1b[0m requests"`. That is a fact about
their terminal, not about evaling, so it is neutralized once here rather than
worked around in every assertion.
"""

import pytest

#: Variables that change how output is rendered rather than what it says.
COLOR_VARS = ("FORCE_COLOR", "COLORTERM", "NO_COLOR", "TERM")


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch):
    for name in COLOR_VARS:
        monkeypatch.delenv(name, raising=False)
