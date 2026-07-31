# Releasing

Checklist for cutting a release. The docs contain a few claims that are true
only before or only after publishing, so they have to move together.

## 1. Docs that go stale on publish

These are accurate today and become wrong the moment the package is on PyPI.
Update them in the same change as the release, not after.

The README, tutorial, and getting-started guide all lead with the from-source
install, because that is the only one that works before publishing. On release
each needs PyPI promoted to the primary path, with the checkout kept as the
contributor route.

| File | Says now | Should say |
| --- | --- | --- |
| `README.md` | "Not yet on PyPI, so for now install from a checkout" | `uv tool install evaling`, checkout as the alternative |
| `docs/getting-started.md` | "Not yet on PyPI, so install from a checkout" | Same |
| `docs/tutorial.md` §1 | Checkout first; "Once evaling is on PyPI this becomes…" | Swap the order; drop the "once published" line |
| `docs/tutorial.md` CI example | `uv tool install evaling` | Correct already — pin a version, e.g. `evaling==0.1.0` |
| `CONTRIBUTING.md` | `uv tool install --force .` | Correct already — the from-source path stays |

Find them all:

```sh
grep -rn "Not yet on PyPI\|not yet on PyPI\|Once evaling is on PyPI" README.md docs/
```

Also state the current version where it helps someone decide whether the docs
they are reading match the package they installed. The README status line and
`docs/getting-started.md` are the two places worth it; everywhere else, the
version in `evaling --version` is enough.

## 2. Version and changelog

- Move everything under `## [Unreleased]` in `CHANGELOG.md` into a dated
  section, e.g. `## [0.1.0] - YYYY-MM-DD`, and leave a fresh empty
  `## [Unreleased]` above it.
- Confirm `version` in `pyproject.toml` matches.
- Tag after merging: `git tag v0.1.0 && git push --tags`, then cut a GitHub
  release pointing at the changelog section.

## 3. Package metadata

- Add `[project.urls]` — Homepage, Repository, Issues, Changelog,
  Documentation. Without it the PyPI page has no links out, which is most of
  how someone evaluates an unfamiliar package.
- Confirm the built metadata still has no email address:

```sh
uv build && python3 -c "
import zipfile, glob
whl = glob.glob('dist/*.whl')[0]
archive = zipfile.ZipFile(whl)
meta = archive.read(next(n for n in archive.namelist() if n.endswith('METADATA'))).decode()
author = [line for line in meta.splitlines() if line.startswith('Author')]
print('author:', author, '| contains @:', any('@' in line for line in author))
"
```

## 4. Publish

- **Run the macOS workflow first.** It is on a weekly schedule (see step 6), so
  the commit you are about to publish may never have been tested there. Actions
  → macOS → Run workflow, against the release commit. Windows and Linux ran on
  the commit itself.
- Reserve the name by publishing. `evaling` was unclaimed as of 2026-07-26,
  and publishing is what claims it — the window between announcing and
  publishing is when someone else can take it.
- Prefer PyPI trusted publishing (OIDC from GitHub Actions) over a long-lived
  API token.
- Smoke test from a clean environment before announcing:

```sh
uv venv /tmp/check && uv pip install --python /tmp/check/bin/python evaling
/tmp/check/bin/evaling --version
cd $(mktemp -d) && /tmp/check/bin/evaling init && /tmp/check/bin/evaling run
```

## 5. Security contact

Enable **private vulnerability reporting** (Settings → Security) *before*
flipping visibility, so the channel exists the moment anyone can read the code.
Add `SECURITY.md` pointing at it.

No email address anywhere — not in `SECURITY.md`, `CONTRIBUTING.md`, or the
package metadata (step 3 checks the last of these). A published address is
scraped within days and cannot be unpublished, and private reporting is a
complete channel without one.

## 6. Repo

- Flip visibility to public.
- Topics are already set (`llm`, `evaluation`, `evals`, `gemini`, …); add
  `ollama` if local models should be part of the pitch.
- Set the homepage field once there is a docs URL or a PyPI page.
- **Restore macOS to every commit.** Actions is free for public repositories on
  standard runners, so the reason it was moved to a weekly schedule disappears
  the moment visibility flips. Delete `.github/workflows/macos.yml`, re-add
  `- os: macos-latest / python-version: "3.12"` to the `include` block in
  `ci.yml`, and update the three places that say macOS runs weekly:
  `README.md`, `CONTRIBUTING.md`, and `REQUIREMENTS.md` (§ "As built").

  Keep `docs.yml` as it is. Its value is not only cost — a markdown-only change
  getting one job instead of eight is faster feedback, which is still worth
  having when the minutes are free.

## Order

Docs and changelog → publish to PyPI → make the repo public → announce.
Publishing before going public means the name is claimed and the install
instructions are true the moment anyone reads them.

## Deferred

- A screenshot of the HTML report in the README (P2).
