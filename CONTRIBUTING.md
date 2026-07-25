# Contributing

Thanks for looking. Bug reports, provider support, and scorers are all
welcome.

## Setup

evaling uses [uv](https://docs.astral.sh/uv/) and needs Python 3.10+.

```sh
git clone https://github.com/amro/evaling && cd evaling
uv sync --group dev
uv run evaling --version
```

## The checks CI runs

Run these before pushing — they're the whole gate:

```sh
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run pytest                # tests
```

`ruff format` also formats Python inside markdown code fences, so a docs-only
change can still fail formatting. Running the exact commands above avoids
that surprise.

The full matrix is Linux on Python 3.10–3.13, plus one macOS and one Windows
job. Windows is not decorative: it has already caught a real
encoding bug that every POSIX job passed.

Faster loop while working:

```sh
uv run pytest -m "not slow"          # skip scale and subprocess tests
uv run pytest tests/test_engine.py   # one file
uv run pytest -k cache               # by name
```

## Expectations for a change

**Tests come with the change.** New behavior gets a test that fails without
it. Bug fixes get a regression test — ideally one that reproduces the bug
before the fix is applied.

**Tests never touch the network.** No real API keys, no live endpoints, no
`time.sleep` to wait for something. Providers are tested through
`httpx.MockTransport`; rate limiters take an injectable clock; the mock
provider covers everything above the transport. A test that needs the network
is a test that will be flaky in someone else's CI.

**Commits are small and individually green.** Each commit should leave the
tree passing on its own, so `git bisect` stays useful.

**Docs change with the code.** README, the relevant page under `docs/`, and
`CHANGELOG.md`. This isn't ceremony — the docs are tested (see below), so
stale docs fail CI.

## The docs are tested

`tests/test_docs.py` enforces what review usually misses:

- YAML examples in the docs must validate against the real config schema
- every command and flag must appear in `docs/cli.md`
- relative links must resolve
- `examples/` referenced from the README and tutorial must exist

The examples in `examples/` are themselves run end to end by
`tests/test_e2e.py`, against the mock provider. Adding an example directory
with an `eval.yaml` is enough to get it covered.

## Adding a provider

1. Subclass `HttpProvider` in `src/evaling/providers/` (or `Provider` if it
   isn't HTTP).
2. Declare `SUPPORTED_MEDIA`, `DEFAULT_API_KEY_ENV`, and `REQUIRES_API_KEY`.
3. Implement `complete()`, returning a `Completion` with text, token counts,
   and cost where the API reports it.
4. Register it in `providers/__init__.py`.
5. Test it with `httpx.MockTransport` and a **realistic** payload — a full
   response body with the fields the vendor actually sends, not a minimal one.
   The point is proving the parser ignores what it doesn't need.
6. Document it in `docs/providers.md`.

Before adding a vendor SDK dependency: the deliberate choice is one HTTP
client for every provider, so retries, timeouts, redaction, and the dependency
audit have one implementation. See [architecture.md](docs/architecture.md).

## Adding a scorer

1. Subclass `Scorer` in `src/evaling/scorers/`, implement `score()`, return a
   `ScoreResult`.
2. Register the type so configs can name it.
3. Test the passing case, the failing case, and malformed input.
4. Document it in `docs/scoring.md`, including its parameters.

## Adding a case source

You don't need to contribute one — a source is your code, loaded by path (see
[no-look.md](docs/no-look.md)). If you're changing the source machinery
itself, note that `iter_source_cases` must never hold more than one page, and
that a source returning a repeated cursor has to raise rather than loop.

## Reporting a bug

Include:

```sh
evaling --version
evaling validate            # does the config itself check out?
evaling --json show latest  # the full run record
```

A run directory (`.evaling/runs/<id>/`) contains the config snapshot and
per-cell results and is usually enough to reproduce a problem — without your
API keys, which are never stored in it.

## Security

Don't open a public issue for a vulnerability. Email the maintainer instead.

Secrets are never read from config files, never written to `os.environ`, and
redacted from output — if you find a path where a key can leak, that's a
security report, not a bug report.

## License

Contributions are accepted under the [MIT License](LICENSE).
