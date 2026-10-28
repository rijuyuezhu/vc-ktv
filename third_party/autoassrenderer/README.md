# Bundled AutoASSrenderer4karaOK processing engine

Source: https://github.com/Volta233/AutoASSrenderer4karaOK
Pinned source commit: 39cedb317c9e11add83e9a72c31368cabd8416d4
License: Apache License 2.0 (see LICENSE in this directory).

The portable upstream model processing components have been copied to top-level
`ktv_app/` with their original module names/import structure so fixes can be
compared against upstream. This is a **library subset**, not the upstream web
application. The upstream `ktv_app/lyrics.py` is intentionally reduced to its
original lyric preprocessing functions; we do not import their online lyrics
retrieval, GUI, DB, installer, or task scheduler.

Upstream is credited as the author of the processing code. New VC KTV-specific
glue goes in `vc_ktv/upstream_pipeline.py`; avoid editing original algorithm
modules without a documented ARM64/integration reason. Model/weight and
third-party package licensing is **separate** from this source license.

For automated upstream checks, the machine-readable source reference is
[UPSTREAM.json](UPSTREAM.json). Keep its `commit` value synchronized when
rebasing the vendored files to a new upstream revision. The accompanying
`assets.lock.json` is a **snapshot** of the upstream pinned model inputs;
do not equate an upstream source commit update with a model upgrade.

## Upstream update notification

[`.github/workflows/upstream-check.yml`](../../.github/workflows/upstream-check.yml)
checks upstream `main` every Monday at **06:20 UTC** against the exact
`commit` in `UPSTREAM.json`. When there is a change, it opens one
`upstream-update` GitHub Issue (or updates the existing open notification)
with a comparison link. Notifications are deduplicated by upstream SHA,
including previously closed notifications. It never updates source or models.

The workflow runs with GitHub's built-in `GITHUB_TOKEN`, with only
`contents: read` and `issues: write`. There is no PAT, external service,
Dependabot configuration, model download or automatic PR merge.

To test from the GitHub Actions UI use **Vendored upstream update reminder**
→ **Run workflow** → check `dry_run` and optionally enter a simulated
40-character upstream SHA. No Issues or labels are modified in dry-run mode.
Equivalent CLI commands from the default branch:

```sh
gh workflow run upstream-check.yml -f dry_run=true -f simulated_head=1234567890abcdef1234567890abcdef12345678
GITHUB_REPOSITORY=rijuyuezhu/vc-ktv python3 scripts/check_upstream.py
```

After manually reviewing a new upstream revision, update `UPSTREAM.json` to
the adopted commit *in the same PR that updates the copied code*. Review the
model asset lock separately if upstream changes its model versions.
