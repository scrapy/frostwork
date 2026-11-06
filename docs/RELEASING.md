# Releasing Frostwork

The release is tag-driven, but the tag is the last local step, not a repair mechanism. Once its version
is on PyPI, a release tag is an immutable public identifier shared by GitHub, PyPI provenance and users'
lock files.

## One-time repository controls

The public settings are part of the release boundary even though they do not live in this checkout:

- PyPI's Trusted Publisher must name owner `scrapy`, repository `frostwork`, workflow `publish.yml`, and
  environment `pypi`.
- The GitHub `pypi` environment must exist, because the Trusted Publisher names it, and it carries no
  required reviewers: creating the release tag is the authorization to publish. Its deployment-branch
  rule, if any, has to admit tags.
- A GitHub repository ruleset should restrict who can create, update and delete tags matching
  `[0-9]+.[0-9]+.[0-9]+`. That restriction is what limits who can publish.

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

`gate-mutate-full` and `soak` also run weekly on `main` (`.github/workflows/scheduled.yml`), so reading
that run covers them unless the engine changed since. `corpus-real` has no such run — it fetches live
third-party pages — and it is the one gate that sees markup nobody here wrote, so the bump refuses to
start until it has passed at the sources this tree is about to release:

```bash
make corpus-fresh                      # what the bump's setup hook runs
make corpus-real                       # stamps fixtures/realweb/.passed with HEAD on success
```

The stamp holds the commit the gate passed at, and `CORPUS_SOURCES` — the engine, the package and the
gate's own two scripts — is what has to match it. A documentation or workflow commit therefore keeps a
stamp valid, while an uncommitted engine edit invalidates it, because the comparison reads the working
tree.

Use a larger real corpus and a meaningful coverage-guided fuzzing budget for parser changes. The
`release-check` part of `make ci` renders the public Markdown and checks repository links and heading
anchors, builds the sdist, validates its Core Metadata with Twine, and rejects relative README links or
stale pre-publication copy. Twine skips Markdown rendering; `tools/check_docs.py` checks it explicitly.

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

Once any file of a version is on PyPI, never delete, recreate or force-move its tag. If the workflow or
metadata is wrong, fix it on `main` and release the next version. Do not delete or yank an installable
PyPI release merely to repair its description; deletion breaks pinned installs, while the next version
becomes the default project page.

If the release fails before anything reaches PyPI, re-tag instead of spending a version nobody could
install. Fix the problem in a commit on top of the release commit, so the version stays `X.Y.Z`, then
move the tag to it. No bump runs, so if the fix touches what the release checks cover, rerun them first,
`make corpus-fresh` included.

```bash
curl -sf https://pypi.org/pypi/frostwork/X.Y.Z/json   # must fail: nothing was uploaded
git push origin :refs/tags/X.Y.Z
git tag -f -a X.Y.Z -m "Bump version: A.B.C → X.Y.Z"
git push --atomic origin main X.Y.Z
```

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

After a release, confirm the PyPI provenance names the expected tag, commit and workflow. The dated
heading may stay at the top of the changelog until the next user-facing change opens its own
`## X.Y.Z (unreleased)` section.
