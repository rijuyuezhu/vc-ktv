# Upstream karaoke pipeline adoption (#26–#29)

## Source and status

Source: [Volta233/AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK) at **39cedb317c9e11add83e9a72c31368cabd8416d4**, Apache-2.0. The upstream attribution and full license are in [third_party/autoassrenderer](../third_party/autoassrenderer/README.md). The model checkpoints and Python environments are **not bundled**; their licensing and Linux ARM64/CUDA setup are handled separately in #28.

**#27 only ports the processing engine and its entry point. The deployed AutoPrep still uses its existing pipeline.** #28 runs real Kim/Qwen/TIFA on Spark-WSL; #29 connects this adapter to the queue and removes the superseded implementation. No user-facing pipeline switch is added yet.

## Files and interface

The copied upstream modules are grouped in top-level `ktv_app/` with original imports: separation, independent model workers, ASR location, verified safe block planning, TIFA, seam recovery, source word ownership, estimated completion, ASS subtitles and FFmpeg rendering. Its project manager, web UI, backend, online lyrics search and Windows installer are **not** included. `ktv_app/lyrics.py` contains only the upstream lyric-preprocessing functions, to preserve ordering and original display text while using VC KTV's existing lyric selection.

`vc_ktv/upstream_pipeline.py:make_karaoke_video(source, confirmed_lyrics, work_dir, settings, *, progress=..., style=..., render_options=...)` is the single entry point. It performs:

1. upstream `media.extract`, including original audio offset and media metadata;
2. upstream `lyrics.preprocess` on the confirmed user lyrics, keeping original display text and repeated lines;
3. upstream Kim vocals for **inference**, not for output;
4. upstream `align_reliable_blocks` (dual ASR, safe **≤90-second** TIFA blocks, local retries);
5. upstream `complete_timeline` with explicit model/estimated provenance;
6. upstream `write_bundle` to make ASS, LRC, JSON timeline and report;
7. upstream `render` using the **original video's audio**, preserving audio streams.

The caller must pass a brand-new attempt directory and explicit `ktv_app.config.Settings`; the adapter returns `PipelineResult` paths for the output MP4, ASS, timeline, report and run metadata. Attempts write `run.json` on success and failure. It does not know the VC KTV database or event IDs. #29 will own event-store pinning, cancellation, download retries, readiness gating and atomic final-media commit.

`ktv_app/config.py` still has **upstream default model paths**. #28 should create Settings with real Spark-WSL paths and package-specific Python environments instead of modifying vendored algorithms or using Windows binaries. No CUDA library import or model download occurs when importing this engine.

## Tests

- Copied upstream algorithm tests under `tests/upstream/`: safe block planning, repeated choruses, ASR anchoring, normalization/source mapping, partial completion and model diagnostics. Omitted upstream API/project-database tests: they depend on the excluded second web application.
- VC KTV adapter tests exercise the *real* FFmpeg source-video → subtitle MP4 path with mocked Kim/TIFA workers; assert lyric fidelity, sound-track bitstream preservation, fresh directories and explicit failure metadata.
- CI and local tests run without GPU or model weights. **Passing these does not mean Kim/Qwen/TIFA have run together on Spark-WSL**; #28 must run the full model chain on a small, authorized input and retain its logs.

Avoid rewriting upstream algorithms unless necessary for ARM64 compatibility or a reproducible issue. Preserve the pinned source commit when comparing future upstream fixes.
