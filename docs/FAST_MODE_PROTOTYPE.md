# Fast Mode — quick lyrics alignment and full karaoke rendering

2026-10-30 Spark-WSL. Tested, opt-in prototype. The standard quality
pipeline remains the default; production can opt in via its service environment.

## Pipeline

Original audio -> Qwen3-ASR-1.7B BF16 batch 4 in two offset passes ->
coarse text-to-lyrics matching -> 4–7 roughly timed overlapping groups ->
one persistent TIFA process for native word alignment in each group ->
limited retries for cross-block overlaps -> complete ASS/LRC and full MP4.

No Kim vocal separation. No separate TIFA timing across 36 Qwen ASR windows.
The full video must be rendered before it is ready; no streamed subtitles.
New renders use H.264 CRF 23 (previously 20); for results over 30 MB,
they are rerendered directly from the original media/ASS with a calculated
bitrate budget (aim ~28 MB), retaining H.264 compatibility and original
resolution. Supported audio streams are copied verbatim. Compression is
best-effort and can take an extra encoding pass; failure retains the first
playable MP4. This is lossy video compression, not lossless pixels; existing
rendered MP4 files are never transcoded automatically.

## Activation

Standalone CLI: pass --mode fast to
python -m scripts.profile_spark_pipeline, alongside --source, --lyrics,
--out, --name cold-fixed, and --tifa-root ~/Code/vc-ktv.

AutoPrep opt-in: set VC_KTV_PIPELINE_MODE=fast in the service environment.
Unset or set quality to retain the established pipeline. No UI was added.

Fast Mode is a *latency optimization*, not a hard three-minute deadline.
Alignment and rendering proceed to completion even if total processing exceeds
180 seconds. ASR, individual TIFA blocks, and FFmpeg retain independent
process-level timeout safeguards (30 minutes for ASR/render and up to 60
minutes per TIFA block), so a stuck tool still fails without permanently
blocking the queue. There is no automatic switch to the slower quality
pipeline. Video length, hardware load, and number of lyric blocks determine
the actual completion time.

## Actual measurements: 笼中少女 (BV15x411F75r)

237.845-second video, 19 user-confirmed lyric lines.

| Metric | Verified quality baseline | Fast prototype final |
| --- | ---: | ---: |
| Local rendering end to end | 345.733–351.608 s | **120.227 s** |
| Confirmed lyric lines | 19 | 19 |
| Native model-timed words | 382 | 382 |
| Estimated or untimed words | 0 | 0 |
| Missing lines | 0 | 0 |
| Encoded audio track MD5 | a707f005f002a361ac306f475fd285ff | identical |

Relative to the earlier validated pipeline's 764 start/end boundaries,
median difference is 0.01 s, p90 0.105 s, p95 0.290 s,
97.5% within 0.5 s. One line (#14) still has a visible discrepancy,
maximum 5.19 s. The earlier result is a practical reference,
NOT human-annotated ground truth.

Earlier fully automatic attempt: 98.17 s but 48 estimated units, so the
final candidate uses targeted repairs instead. The final 120.23-second
measurement narrowly misses the ideal of under 120 seconds, but is
below 3 minutes in this test.

## Repro / raw outputs (Spark-WSL only)

- Verified baseline:
  ~/.local/share/vc-ktv-pipeline-staging/profiles/opt45-bv15x411f75r-20261028T175218Z/
- Fast final, logs, metrics, complete timeline and MP4:
  ~/.local/share/vc-ktv-pipeline-staging/profiles/fast-mode-final-20261029T182156Z/
- Raw exploratory tradeoffs:
  ~/.local/share/vc-ktv-pipeline-staging/profiles/fast-tifa-tradeoff-20261029T174922Z/

Within the final run inspect cold-fixed/metrics.json,
cold-fixed/work/alignment/block_plan.json,
cold-fixed/work/subtitles/timeline.json and
cold-fixed/work/subtitles/export_report.json.
Copyrighted media stays on Spark-WSL and out of git.

## Limits

This was an informal single-song smoke test. Repeated choruses, alternate
lyric versions, other languages, and missing lyrics may degrade timing.
The pipeline keeps source provenance for each window and reports estimated
timestamps if completion ever has to interpolate. It may explicitly fail
when ASR evidence cannot be mapped to the lyrics.

No production deployment or automatic switch of existing jobs.
