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

## Lyrics-source extension

VC KTV issue #36 also vendors the upstream `lyric_names.py`,
`lyric_netease.py`, `lyric_sources.py`, and `web_search.py` from
the same pinned source commit in `UPSTREAM.json`. These components only
fetch **possible source lyrics**; VC KTV's guest explicitly selects the
version. We keep our own VCPedia retrieval and existing guest ticket logic.

## VC KTV #45 performance-specific deviations from pinned upstream

The pinned upstream reference stays unchanged. The VC KTV adaptation in
`ktv_app/aligners/` now:
- passes a single existing `TIFASession` through original-lyric TIFA blocks
  and seam retries (model/G2P process reuse, same block order/ownership checks);
- skips TIFA timestamps for only Chinese ASR chunks that have **no contiguous
  two-character match** anywhere in the user's confirmed lyric lines, retaining
  all independent double-pass Qwen ASR text and falling back to full TIFA
  timing when verified cuts are insufficient;
- writes lightweight per-TIFA-task G2P, dataset and inference timing traces.

These are local performance modifications, not changes to upstream model
weights or authorization to replace source-verified word timestamps. For
same-video, same-lyrics validation and the versioned raw profile location see
`docs/PERFORMANCE_TIFA_OPTIMIZATION.md`.

## VC KTV #32 post-migration dead project-UI cleanup

Relative to the **unchanged** pinned source `39cedb317c9e11add83e9a72c31368cabd8416d4` (checked against GitHub files at that commit), `ktv_app/workflow.py` now retains the `AlignmentOptions` validated CUDA/Kim/TIFA contract but drops its unused former project-step/UI reporting helpers: `steps`, `alignment_identity`, and `timing_evidence`. Those functions were not imported by the `vc_ktv/` adapter, `ktv_app/`, scripts, or tests. The upstream module was already adapted in earlier integration to remove the unused fixed `max_tifa_input_seconds=90` input; that change is **not** introduced here.

`ktv_app/config.py` removes its unused project-storage root (`Settings.projects` / `KTV_PROJECTS`) and old web lyrics API knob (`Settings.lyrics_url` / `KTV_LYRICS_API`), neither read by the VC KTV engine, application, or tests. Existing asset/runtime paths, `Settings.tool()`, FFmpeg, TIFA/Kim/Qwen controls and platform path handling are untouched. The actual guest lyrics search uses `vc_ktv/lyric_selection.py`, not this unused upstream settings field.

This cleanup removes dead UI/project code only; it does **not** consolidate stage-specific process launches or caches. The existing separation/Qwen/TIFA stages have distinct runtime environments, cache identities, failure evidence and latency considerations; combining them without a measured reduction in duplicated work would expand scope and risk regressions. Windows-compatible pinned-source defaults likewise remain, rather than adding a Linux-only fork. Subsequent upstream merges must review these two local reductions and retain the historical performance/alignment deviations documented above. No upstream pointer or model assets were changed.
