# Alignment baseline and upstream decision record

Status: P0 infrastructure and source audit (2026-10-28). **No verified real-singing accuracy results yet.** This document is not a claim that VC KTV matches the upstream model pipeline.

## Why this comparison matters

VC KTV is an on-demand, Bilibili BV-first service: guests submit and confirm lyrics while the background queue prepares a playable video. Our effective outcome is not just “a TextGrid exists”: a prepared song needs faithful displayed lyrics, acceptable alignment timing, and a completed MP4 **before its turn to play**. The default per-event subtitle gate excludes unrendered originals.

Reference implementation: [Volta233/AutoASSrenderer4karaOK at 39cedb3](https://github.com/Volta233/AutoASSrenderer4karaOK/tree/39cedb317c9e11add83e9a72c31368cabd8416d4) (Apache-2.0), especially [PIPELINE.md](https://github.com/Volta233/AutoASSrenderer4karaOK/blob/39cedb317c9e11add83e9a72c31368cabd8416d4/docs/PIPELINE.md), [worker.py](https://github.com/Volta233/AutoASSrenderer4karaOK/blob/39cedb317c9e11add83e9a72c31368cabd8416d4/ktv_app/aligners/worker.py), [reliable_blocks.py](https://github.com/Volta233/AutoASSrenderer4karaOK/blob/39cedb317c9e11add83e9a72c31368cabd8416d4/ktv_app/aligners/reliable_blocks.py), [completion.py](https://github.com/Volta233/AutoASSrenderer4karaOK/blob/39cedb317c9e11add83e9a72c31368cabd8416d4/ktv_app/completion.py). Audit baseline is fixed, not floating main.

## Stage-by-stage source-linked map

| Stage | VC KTV HEAD (post #15/#16) | Reference | Decision |
| --- | --- | --- | --- |
| Recording and lyrics | BV video download, VCPedia/LRCLIB candidate chosen by guest, original audio retained ([autoprep.py](../vc_ktv/autoprep.py), [lyric_selection.py](../vc_ktv/lyric_selection.py)) | User-supplied MV or audio+picture, confirms recording version, broader source fallback | **Keep** BV-first selection; assess search fallback separately |
| Audio | FFmpeg extracts mono 48 kHz from mixed video | Kim Mel-Band Roformer vocal separation, checks same clock/duration | **Evaluate** after same-song baseline; no dependency added |
| Lyrics pronunciation | Deterministic known scat mappings, original source spans and logs; unknown language may still fail ([autoprep.py](../vc_ktv/autoprep.py)) | Partial G2P traces, omitted words preserved ([partial_g2p.py](https://github.com/Volta233/AutoASSrenderer4karaOK/blob/39cedb317c9e11add83e9a72c31368cabd8416d4/ktv_app/aligners/partial_g2p.py)) | **Adopted minimal mapping** in #15; revisit partial word recovery with #19 |
| Song-version grounding | Pass entire confirmed lyrics directly to TIFA | Two-pass Qwen3-ASR, TIFA window locator, lyric-version anchoring | **Evaluate** #20 with repeated/missing verses; ASR is not timing truth |
| Acoustic alignment | One whole-song TIFA, output TextGrid | Verified safe boundaries, formal TIFA blocks <90s, seam fixes and local retries | **Adapt conditionally** #19 after #18 baseline; never cut blindly |
| Output timeline | Native words -> enhanced LRC -> ASS/libass -> MP4; no estimation ([textgrid_import.py](../vc_ktv/textgrid_import.py)) | Native indices, `model/manual/estimated` provenance, bounded completion | **Defer estimation** until explicit source flags and acceptability policy exist |
| Failure and replay | Per-attempt archived logs and subtitle-only retry, default subtitle gate (introduced in #15) | Localized failures with explicit omitted/estimated provenance; genuine resource errors remain errors | **Keep** current gate; test effects before expanding |
| Review and UI | Minimal guest/admin queue, no editor | Offline editor/review workflow | **Defer** rich editor; may add concise admin diagnostics if a measurement shows need |

The upstream pipeline is **Kim → two-pass short-window Qwen3-ASR → TIFA window locator → supported cuts → block TIFA/local repair → explicitly marked completion**. It does **not** apply Qwen ForcedAligner on each song. Neither its filled timestamps nor our unit tests are evidence of real singing accuracy.

## Experiment inputs and evidence

Use small, lawful test recordings whose recording and lyric usage is authorized. No production session media, cookies, private accounts, live SQLite, or unlicensed fetched songs. At minimum cover:
1. Small synthetic speech control (existing committed `tests/fixtures/tifa-espeak-hello.TextGrid` validates the converter but is **not** a full-song inference control).
2. Chinese/VC sung phrase with backing track and human-annotated word starts/ends.
3. Repeated chorus and skipped/shortened lyric verse.
4. `DUDUDU` / `DaLaDa`, mixed Chinese/English and version mismatch.

For every sample record a permission note, **exact WAV bytes and lyric version hashes**, utterance time reference in seconds, manual word boundaries where available, model source commit and SHA256, PyTorch/CUDA/FFmpeg versions, command arguments, clocks, failed segments, and lyrics-display fidelity. Human onset/offset labels are required before quantifying alignment error; process exit success is only an availability measure.

Acceptance metrics (not yet measured):
- **Availability**: percent of requested songs producing *valid* TextGrid and final MP4, broken down by failure type.
- **Coverage**: proportion of confirmed words with retained native timestamps; count missed/repeated spans separately.
- **Timing**: median and p90 absolute onset/offset error in milliseconds versus annotated recording truth; report word/line definitions.
- **Latency and capacity**: cold/warm total preparation (download, GPU alignment, render), GPU peak allocated/resident MiB per stage, model downloads and install disk budget; compare to *time until song is due*.
- **Fidelity**: preserve exact source words, including scat, chorus order, and original soundtrack. Estimated intervals must be reported separately and cannot count as accurate model timestamps.
- Report **N**, song mix, number of trials, operating conditions and unknowns; no invented accuracy thresholds.

## Isolated, repeatable baseline runner

`scripts/alignment_baseline.py` accepts a local **authorized** WAV and confirmed plain TXT, an existing frozen TIFA checkout/model/Python executable and a brand-new output directory. It refuses to overwrite an existing run, has no KTV DB/network/API integration, and only runs when an operator supplies `--confirm-rights`. It copies the input WAV into that new directory beside the same-stem TIFA lyric text (never alters the original input), runs a whole-song baseline, archives `tifa.log`, raw TextGrid, lyric and model hashes, exact command, elapsed time, optional converted LRC, and `result.json`.

Invoke from the repository root with an already installed TIFA environment; replace all placeholders with explicit absolute paths:

```bash
python -m scripts.alignment_baseline \
  --audio /research/fixtures/authorized-song.wav \
  --lyrics /research/fixtures/authorized-song.txt \
  --tifa /isolated/TIFA --model /isolated/models/TIFA-1.0-ST/model.pt \
  --python /isolated/tifa-env/bin/python \
  --out /research/results/sample01-raw --mode raw --confirm-rights
```

Use a *different* output directory and `--mode pronunciation` to compare the post-#15 deterministic mappings on the same input. Never treat either option as a substitute for independent manual timing verification. Runner records `gpu_memory_peak_mib: null` and `manually_labelled_accuracy: false` deliberately; these measurements are not implemented and **must be collected separately**, not guessed from model logs.

Results are intentionally stored outside Git and outside production media; only tests and this protocol are committed. An isolated run may take substantial GPU time. Current setup has a TIFA model/runtime in the separately ignored production checkout, not in this fresh worktree, and **no authorized real-singing fixture was used** in this P0 audit.

## Next decisions (not auto-approved)

1. Finish #18 by gathering authorized annotated fixtures and a **real** reproducible direct-TIFA result (both raw/pronunciation), with measured timing/coverage. Missing fixture = documented blocker, not a fake success.
2. If #18 establishes an on-demand benefit, #19 should be one end-to-end feature PR for safe bounded blocks, owned word intervals, local recovery, provenance and observable readiness; not a dozen helper micro-PRs. Go/no-go remains open.
3. #20 evaluates Kim and Qwen3-ASR *independently* on the same audio and reference labels. Report resource cost and whether the extra stages help enough to justify a new cohesive feature.
4. Keep real deployment a separate explicit approval after CI and isolated media checks.
