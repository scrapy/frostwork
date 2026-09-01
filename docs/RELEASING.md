# Releasing Frostwork

The release is tag-driven, but the tag is the last local step, not a repair mechanism. A release tag is
an immutable public identifier shared by GitHub, PyPI provenance and users' lock files.

## One-time repository controls

The public settings are part of the release boundary even though they do not live in this checkout:

- PyPI's Trusted Publisher must name owner `scrapy`, repository `frostwork`, workflow `publish.yml`, and
  environment `pypi`.
- The GitHub `pypi` environment should require a maintainer's approval.
- A GitHub repository ruleset should prevent updates and deletion of tags matching `[0-9]+.[0-9]+.[0-9]+`
  and restrict who can create them.

Review those settings when a maintainer leaves. Trusted Publishers belong to the PyPI project rather than
to the account that originally configured them.

## Prepare the candidate

Write the user-facing changes under a `## X.Y.Z (unreleased)` heading in `CHANGELOG.md`, then run the
release checks documented in [TESTING.md](TESTING.md):

```bash
make ci
make gate-mutate-full
make soak
make corpus-real
```

Use a larger real corpus and a meaningful coverage-guided fuzzing budget for parser changes. The
`release-check` part of `make ci` builds the sdist, validates its Core Metadata, asks Twine to render the
long description strictly, and rejects relative README links or stale pre-publication copy.

## Create and push the release

Run the bump matching the prepared changelog version:

```bash
bump-my-version bump patch    # or minor / major
git show --stat --decorate HEAD
git cat-file -t X.Y.Z         # must print: tag
git push --atomic origin main X.Y.Z
```

`bump-my-version` updates `Cargo.toml`, `Cargo.lock` and `pyproject.toml`, dates the changelog heading,
commits, and creates the annotated `X.Y.Z` tag. The atomic push makes the release commit visible on
`main` at the same time as the tag. Push only the exact release tag; `--follow-tags` can publish another
local annotated tag unintentionally.

Never delete, recreate or force-move a published tag. If the workflow or metadata is wrong, fix it on
`main` and release the next version. Do not delete or yank an installable PyPI release merely to repair
its description; deletion breaks pinned installs, while the next version becomes the default project
page.

## What automation proves

`.github/workflows/publish.yml` performs these stages in order:

1. Validate that the source version and dated changelog match an annotated tag contained in `origin/main`.
2. Reuse the complete `.github/workflows/ci.yml` correctness workflow at the tagged commit.
3. Build the full abi3 wheel matrix and sdist, then install and test representative artifacts.
4. Validate the actual sdist metadata and README rendering.
5. Upload through PyPI Trusted Publishing with the `pypi` environment and an ephemeral OIDC credential.
6. Fetch the public PyPI JSON, install the exact wheel from `pypi.org`, and run a minimal extraction.
7. Create the GitHub Release from the matching changelog section only after public verification passes.

A manual `workflow_dispatch` runs the gates and artifact builds but skips publishing, public verification
and GitHub Release creation.

After a release, confirm the PyPI provenance names the expected tag, commit and workflow. Add the next
`## X.Y.Z (unreleased)` changelog heading before accumulating more user-facing changes.
