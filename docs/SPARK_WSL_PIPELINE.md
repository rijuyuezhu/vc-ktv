# Spark-WSL model pipeline

This is the **isolated setup and smoke-test path** for adopted upstream
AutoASSrenderer4karaOK components (#28). It does **not** deploy VC KTV, change
the current `vc-ktv.service`, or access an active event DB.

The copy is pinned by
[`third_party/autoassrenderer/UPSTREAM.json`](../third_party/autoassrenderer/UPSTREAM.json).
Exact upstream model asset URLs and SHA256 digests were copied from the upstream
[`assets.lock.json`](../third_party/autoassrenderer/assets.lock.json).
The automation to *notify* about newer upstream commits is tracked in #31;
model weights are **not** automatically upgraded.

## Linux ARM64 CUDA environment

In the repo checkout (Spark-WSL):

```sh
uv sync --group dev
uv run --no-sync python -m scripts.setup_spark_assets all
uv run --no-sync python -m scripts.setup_spark_runtimes all
```

These commands create/download **only** under
`~/.local/share/vc-ktv-pipeline-staging/`. They do not copy anything into Git,
the active VC KTV database, or its `.runtime`.

- Kim source and checkpoint are pinned to upstream commit/checkpoint hashes.
- Qwen3-ASR-1.7B comes from the upstream-pinned Hugging Face revision; the
  downloader tries `hf-mirror.com` first and resumes partially downloaded
  files, then checks SHA256 before accepting them.
- The existing working TIFA checkout/model/Python are referenced read-only.
  Its source revision on this machine is
  `614a277d2580efe5e4b84faf4c34753dbc5a062c`.
- Kim and Qwen run in separate uv Python 3.12 overlays, reusing the known-good
  RTX Spark CUDA PyTorch 2.13.0+cu130 installation. They do not install
  conflicting Torch wheels. The Qwen overlay pins `tokenizers==0.22.2` for
  `transformers==4.57.6`; ARM64 `DyNet38==2.2` supplies the Nagisa runtime
  imported by qwen-asr even when no Qwen ForcedAligner is called.
- Keep the environment on Linux ARM64; upstream Windows `install.cmd` and
  binary packages are not portable. Qwen model source/weights have Apache-2.0
  metadata; the independently distributed Kim source/checkpoint must be
  checked against its own terms, not treated as covered by our vendored
  upstream Apache-2.0 license.

Use an **authorized** video + confirmed matching lyrics and a *new* output
directory:

```sh
uv run --no-sync python -m scripts.run_spark_pipeline \
  --source /absolute/path/authorized-song.mp4 \
  --lyrics /absolute/path/confirmed-lyrics.txt \
  --out /absolute/path/new-attempt
```

The wrapper calls upstream `media.extract`, Kim, dual-pass Qwen3-ASR,
bounded TIFA and seam recovery, source-preserving completion, ASS/LRC,
and MP4 render with original MV audio. Each step's logs and artifacts stay
under its own attempt; `run.json` indicates the final outcome. The entry
point is also callable through
`vc_ktv.upstream_pipeline.make_karaoke_video`.
The live event-store / playback integration comes in #29.

## Actual GPU smoke run (2026-10-28)

On RTX Spark N1X, an isolated **4.73-second ACE-KiSing real-singing clip**
(CC BY-NC 4.0) was combined with a plain-colored 640×360 video, then aligned
with the manually verified lyric text "请记得记得曾是怎样路过".
This is a real model-run check, **not** an upstream MV/whole-song accuracy
benchmark.

- Kim CUDA FP16 vocal extraction completed and preserved the input's timing.
- Qwen two ASR offset passes both recognized
  "请记得，记得曾是怎样路过。" independently.
- TIFA produced **11 model-sourced** word/character intervals; no estimated
  units, missing lines or subtitle export warnings.
- ASS and MP4 generated; FFprobe reports the same **4.730-second** duration,
  H.264 video and AAC audio. The input/output AAC audio packet MD5s were both
  `d4a5fe2280b7fb8f07ab3dfa9add1748`, proving the original soundtrack
  was retained byte-for-byte through the remux.
- Isolated logs and media:
  `~/.local/share/vc-ktv-pipeline-staging/smoke/attempt-002/`.
  The first failed attempt is kept separately: upstream GPU probe used
  `Path(python).resolve()`, which bypasses uv venv symlinks. The compatibility
  fix retains the `venv/bin/python` entry point and replaces Windows-only
  setup error messages.

Long-song safe-block planning and genuine Bilibili user/queue playback remain
to be accepted in #29. Upstream's focused planner/seam tests continue to
cover the split/recovery logic; no research-scale benchmark is needed.
