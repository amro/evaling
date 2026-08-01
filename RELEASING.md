# Releasing

Cutting a release is four steps. Publishing is automated: a **published GitHub
Release** triggers `.github/workflows/publish.yml`, which builds and uploads to
PyPI over OIDC. There is no token to manage and nothing to upload by hand.

## 1. Version and changelog

```toml
# pyproject.toml
version = "0.2.0"
```

```python
# src/evaling/__init__.py
__version__ = "0.2.0"
```

Both, and they must agree — `pyproject` is what PyPI publishes under, while
`__version__` is what `evaling --version` and `doctor` report. A test compares
them, so forgetting one fails CI rather than shipping a package that
misreports itself.

In `CHANGELOG.md`, retitle `## [Unreleased]` as `## [0.2.0] - YYYY-MM-DD` and
leave a fresh empty `## [Unreleased]` above it.

The version and the tag must match — `publish.yml` refuses the release if they
disagree, because the tag is what a human typed and the version is what
actually ships. Merge this, and wait for CI to go green on `main`.

## 2. Tag

```sh
git tag v0.2.0
git push origin v0.2.0
```

A tag on its own publishes nothing. That is deliberate: an accidental tag push
should not be able to ship a package.

## 3. Release

```sh
gh release create v0.2.0 --title "v0.2.0" --notes "See CHANGELOG.md."
```

This is the irreversible step. It fires `publish.yml`, which checks the tag
against the version, builds, runs `twine check`, and uploads. Watch it:

```sh
gh run watch $(gh run list --workflow=Publish --limit 1 --json databaseId -q '.[0].databaseId')
```

If it fails, nothing was published and the version number is still free. If it
succeeds, that version is permanent — PyPI lets you yank a release but never
reuse the number.

## 4. Smoke test what you actually published

```sh
uv venv /tmp/check && uv pip install --python /tmp/check/bin/python evaling
/tmp/check/bin/evaling --version
cd $(mktemp -d) && /tmp/check/bin/evaling init && /tmp/check/bin/evaling run
```

The test suite runs against the working tree; this is the only thing that
exercises the built artifact — wrong packaging, a missing data file, a broken
entry point. Worth the thirty seconds.

## Notes

**Semver, pre-1.0.** The config format is still allowed to shift. Breaking
changes go in the minor position (0.1 → 0.2) and are called out at the top of
the changelog section, since that is the only warning anyone gets.

**The docs are tested**, so a stale claim fails CI rather than shipping. If a
doc says something that is true only before or only after a release, it has to
move in the release commit itself.

## One-time setup, for reference

Already done for this project; recorded in case it is ever needed again.

- **PyPI trusted publishing.** On PyPI: *Publishing* → a publisher for owner
  `amro`, repository `evaling`, workflow `publish.yml`, environment `pypi`. The
  matching `pypi` environment exists under repository Settings → Environments.
  No API token is stored anywhere.
- **Security reports** go through GitHub private vulnerability reporting
  (Settings → Code security), which `SECURITY.md` points at. It is offered for
  public repositories only, so it cannot be enabled before a repo goes public.
- **Secret scanning and push protection** are on, which is worth keeping: push
  protection blocks a commit containing a credential before it lands, and this
  is a tool whose failure mode is leaking one.
