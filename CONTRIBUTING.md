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

`uv run evaling` always reflects your working tree. If you'd rather type
`evaling`, install it — but note that `uv tool install` copies the code, so
re-run it after every change:

```sh
uv tool install --force .
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

### Fuzzing

`tests/test_config_fuzz.py` mutates a valid config — structurally, and at the
byte level — and asserts that loading it either works or raises `ConfigError`
with a message. Anything else is a bug: a user cannot act on a Pydantic dump
or a codec traceback, and it reads as evaling breaking rather than their file
being wrong.

Seeds are fixed, so a failure reproduces from its parameter id alone. If you
add a loader for a user-authored file, add it to `TestTheOtherLoaders`.

### Mutation testing

The recurring failure in this project is a test that passes with the code it
guards deleted. Mutation testing finds those automatically: it changes the
source and checks whether anything notices.

```sh
uv run --with mutmut python -m mutmut run     # ~1 min
uv run --with mutmut python -m mutmut results # what survived
uv run --with mutmut python -m mutmut show evaling.privacy.x_scrub_secrets__mutmut_14
```

Scoped, in two ways, both in `[tool.mutmut]` in `pyproject.toml`. Only the
modules that encode invariants are mutated — that is where an untested
guarantee does real damage. And the few tests that change directory are
excluded, because mutmut resolves its own relative paths inside each test.
That second list is an *exclusion* list on purpose: the inclusion list it
replaced went stale the first time a test file was added, and 17 survivors
turned out to be one function whose tests simply were not being run.

**A surviving mutant is a question, not a bug.** Triage the list rather than
trusting it. Classes established as equivalent, so you can skip them:

| Survivor | Why it cannot be killed usefully |
|---|---|
| `round(x, 6)` → `round(x, 7)` | Below any precision the data carries. |
| `json.dumps(..., sort_keys=True)` → `False` | Key order in a log line; no behaviour claims it. |
| Prose changed or case-flipped | Killing these means asserting exact message text, which makes messages unimprovable. Assert fragments. |
| `.get(key, default)` default changed | The schema guarantees the key. Defensive code. |
| `if total_weight == 0:` branch | Weights must be positive, so the branch is unreachable. |

What triage has actually found, and what a real gap looks like: the sign on
the overall pass-rate delta (`b - a` → `b + a`) survived the whole suite, as
did `>=` → `>` on `min_score` — a run scoring exactly its threshold. Both
were cases where every existing test sat comfortably away from the boundary.

CI runs this weekly and reports; it is not a gate. Check the run for
`segfault` and `Failed to run clean test` before reading the survivor count —
mutmut reports those separately from failures, and a broken run can look like
a clean sheet.

### Performance guards

`tests/test_performance.py` (marker: `perf`) asserts that memory stays flat as
cells increase and that model calls actually overlap. CI gives it its own job
with coverage off, since instrumentation overhead swamps a timing measurement.

They assert *shape*, not absolute speed: a shared runner has no reliable
cells-per-second, but "four times the cells costs about four times the time"
holds anywhere. Bounds are loose on purpose — they catch a 10x regression, and
a guard that fails on a busy laptop gets deleted rather than fixed.

## Expectations for a change

**Tests come with the change.** New behavior gets a test that fails without
it. Bug fixes get a regression test — ideally one that reproduces the bug
before the fix is applied.

**Tests never touch the network.** No real API keys, no live endpoints, no
`time.sleep` to wait for something. Providers are tested through
`httpx.MockTransport`; rate limiters take an injectable clock; the mock
provider covers everything above the transport. A test that needs the network
is a test that will be flaky in someone else's CI.

This one is enforced rather than trusted, because it was quietly broken for a
while. `tests/conftest.py` strips every `*_API_KEY` variable and
`EVALING_SECRETS` from the environment, so your exported key cannot be spent
by a test run, and it gives any HTTP client built without an injected
transport one that refuses to send:

```
RefusedRequest: this test tried to reach https://api.example.com/v1 for real.
```

If you see that, stage the call instead — `provider._client =
httpx.AsyncClient(transport=httpx.MockTransport(handler))`, or use the mock
provider. `tests/test_suite_isolation.py` covers the guards themselves.

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
evaling --json doctor       # version, settings and where each came from, providers
evaling --json show latest  # the full run record, if a run is involved
```

`--json` is a global flag, so it goes before the command.

`doctor` reports which secrets files it found and which variables they define,
by name — never their values — so its output is safe to paste.

A run directory (`.evaling/runs/<id>/`) contains the config snapshot and
per-cell results and is usually enough to reproduce a problem — without your
API keys, which are never stored in it.

## Releasing

See [RELEASING.md](RELEASING.md). Several doc claims are true only before or
only after publishing, so they have to change in the same commit as the
release.

## Security

Don't open a public issue for a vulnerability. Email the maintainer instead.

Secrets are never read from config files, never written to `os.environ`, and
redacted from output — if you find a path where a key can leak, that's a
security report, not a bug report.

## License

Contributions are accepted under the [MIT License](LICENSE).
