# Subtitle alignment: practical notes

**Historical smoke-test snapshot; the earlier decision to defer Kim/Qwen was superseded by [#26](https://github.com/rijuyuezhu/vc-ktv/issues/26).** The adopted upstream implementation and migration status are now tracked in [UPSTREAM_PIPELINE.md](UPSTREAM_PIPELINE.md). The old direct-TIFA route remains deployed until #29.

VC KTV is a **self-hosted KTV app**, not an alignment research project.
The goal is to make queued videos with readable subtitles before their turn,
without turning routine requests into an expensive multi-model pipeline.

## Current pipeline and upstream comparison

- **VC KTV:** user-selected BV + lyrics → mixed audio → full-song TIFA → enhanced LRC → ASS → MP4. Known scat such as DUDUDU gets a Chinese pronunciation hint but retains original display text. Failures retain source media and support subtitle-only retry.
- **[AutoASSrenderer4karaOK @ 39cedb3](https://github.com/Volta233/AutoASSrenderer4karaOK/tree/39cedb317c9e11add83e9a72c31368cabd8416d4):** vocal separation (Kim) → dual-pass Qwen3-ASR anchors → safe TIFA blocks and local retry → explicit recovered/estimated timestamp provenance. Stronger offline correction, but more models, setup and runtime.
- **Decision:** keep our simpler pipeline. No unconditional Kim, Qwen, automatic estimated timestamps, or large editing UI. Revisit local block recovery only if actual full-song failures become common; don't invent cut points.

## Small real-singing smoke test (2026-10-28)

Ran three short sung clips on Spark-WSL with existing TIFA and CUDA.
Source: [ACE-KiSing validation](https://huggingface.co/datasets/espnet/ace-kising-segments), CC BY-NC 4.0.
Used the dataset's **reference phoneme sequence** as explicit TIFA PFML input and compared the resulting TextGrid phone tier to reference phoneme timestamps.
This checks acoustic alignment only; it is **not** a BV-to-MP4 end-to-end test, and all three clips are from one singer.

| Clip | Duration | Matched phones | Mean onset/end absolute error | Notes |
| --- | ---: | ---: | ---: | --- |
| kising_yunhao_442_011 | 4.73 s | 21/22 (95.5%) | 88 ms | One skipped ang |
| kising_yunhao_442_018 | 8.97 s | 38/40 (95.0%) | 91 ms | Two skipped phones; ~12.7 s wall time |
| kising_yunhao_443_010 | 15.74 s | 80/80 (100%) | 88 ms | ~11.2 s wall time |

All three inference runs exited normally and emitted TextGrid. Some phones were skipped despite successful exit; future subtitle fixes should treat missing units as explicit failures, not invisible successes. These numbers are a quick smoke check, not a song-level accuracy or latency claim.

Isolated data on Spark-WSL: /home/user/Playground/vc-ktv-alignment-benchmark (downloaded ~25 MB validation shard; sample audio, reference labels, PFML, TIFA logs/TextGrids and GPU samples). No production songs or media were used.

## When something fails in practice

1. For a failed song use the administrator's **retry subtitles** action, without redownloading. Look at the attempt-specific tifa.log and attempt.json.
2. Check that selected lyrics match the recording; retry does not repair incorrect song versions.
3. If recurring misses appear, open a narrow bug report with a reproducible lyric/audio snippet and the failure logs. Only then consider #19 (local block recovery).
4. Extra Kim/Qwen models are deferred until simple TIFA visibly fails in real use. Avoid adding them just to match upstream.

For isolated experiments there is an optional scripts/alignment_baseline.py (authorized WAV/TXT; frozen TIFA checkout/model; new output directory; --confirm-rights). It copies audio and same-stem lyrics to a fresh directory, saves raw logs and TextGrid, and leaves production untouched. Routine VC KTV development does **not** need to invoke it.
