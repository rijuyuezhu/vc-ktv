# Quality pipeline: three-song throughput comparison

Hardware: Spark-WSL, RTX Spark N1X (64,000 MiB GPU-visible memory), 20 ARM CPU cores. Inputs: three archived original MP4s and historical user-confirmed lyrics, same model weights as PR #80, no production DB modifications. All stages use **Kim + Qwen3-ASR dual-pass + every nonempty TIFA ASR window + TIFA verified lyric blocks, seam correction and FFmpeg**. Independent fresh work directories and raw per-stage GPU samples; no Fast Mode shortcuts.

| Song | Serial quality PR #80 | 3 complete pipelines in parallel |
|---|---:|---:|
| BV1jv411w7q6 冻夏 | 380.891 s | 532.953 s |
| BV1gs411o7XG hello&bye,days | 427.129 s | 581.745 s |
| BV1Ls411m76o 回音 | 428.735 s | 593.730 s |
| **Batch wall time / time to finish all three** | **1,236.755 s** | **594.176 s** |
| **Time per completed song for this batch** | **412.252 s** | **198.059 s** |

3-way batch throughput = **2.08×** versus the original serial quality pipeline (about +108% songs/hour). Individual song wall latency rises due to contention; optimizing the *batch*, not each job's latency. Peak reported GPU used memory **14,419 MiB** (of 64,000 MiB), sample-average GPU utilization **51.3%**. Three concurrent GPU model processes were run without altering model precision, scheduling logic, ASR transcripts or timestamps.

**Correctness:** all three rendered MP4 files are **SHA256 identical** to serial output; full two-pass Qwen output, TIFA native timing, final lyric text, word/phoneme timing and provenance are unchanged after ignoring randomly generated internal IDs. Estimated lyrics count stays 0/28, 5/36, 0/28 respectively.

Raw serial: `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr80-archive-three-bv/`.
Raw direct 3-way: `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr83-concurrent-three/`.

## Stage-aware Kim GPU admission experiment

The Kim stem model was identified as GPU-heavy. A separate experiment enables `VC_KTV_KIM_SINGLE_FLIGHT=1` to allow only one concurrent Kim subprocess through an interprocess lock, while keeping ASR/TIFA and encoding of other songs concurrent. It does not alter the model, input or output. Enable only if measured end-to-end batch throughput improves; default remains disabled. Raw samples: `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr83-three-serial-kim/`.

### Measured staged-Kim result

| Song | Direct 3-way GPU execution | Kim single-flight, other stages concurrent |
|---|---:|---:|
| 冻夏 | 532.953 s | 424.638 s |
| hello&bye,days | 581.745 s | 508.394 s |
| 回音 | 593.730 s | 480.087 s |
| **Complete all three** | **594.176 s** | **508.609 s** |
| **Batch time per completed song** | 198.059 s | **169.536 s** |

Serial quality baseline = 1,236.755 seconds. Kim single-flight + 3 song workers gives **2.43×** throughput versus serial, and **16.8% more throughput** than unregulated three-way GPU execution (14.4% shorter batch wall). Peak reported GPU used memory **14,419 MiB**, mean sampled GPU utilization **47.2%**; lower GPU utilization did not imply lower actual batch throughput.

**Output parity is exact:** all three final MP4 SHA256 values are identical to serial; ASR+TIFA first/second-pass native tokens and timings match; final lyric/word/phoneme timing and provenance match after removing only generated internal IDs. Estimated-line counts unchanged (0/28, 5/36, 0/28).

The admission mechanism only wraps the **Kim CUDA worker subprocess** with a process-safe `flock`; FFmpeg audio extraction/conversion, Qwen inference, TIFA, lyric planning and video encoding are not serialized. Locks release on subprocess completion or exception, including worker termination. Lock file remains inside the official pipeline data root; `VC_KTV_KIM_SINGLE_FLIGHT` defaults OFF, and production remains unchanged.

### Limitations

Single run of three fixed songs on an otherwise idle Spark-WSL; performance depends on song durations, GPU load, CPU pressure and when the queue starts. This benchmark proves output parity for these inputs, not universal deterministic behavior under all GPU loads. Even higher concurrency needs a separate acceptance run. No source files, event state or production config were modified during the benchmark.

## Other infra direction evaluated: Lightning `Trainer` reuse

An isolated six-window experiment used identical native TIFA `input.wav`/word requests from the serial benchmark. Baseline (fresh Lightning Trainer per window) = **28.328 s**, experimental single Trainer reused across all six windows = **27.102 s** (only **1.045×** speedup), while all six output `input.TextGrid` files matched exactly byte-for-byte. This is a small gain including shared model startup, not proof of a substantial end-to-end throughput improvement. **Not included** in production code or this PR; the added lifecycle/callback state complexity is not justified by this small sample. Raw data and the experiment-only monkeypatch (outside repository): `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr84-tifa-trainer-reuse-experiment/`. Future optimization should profile Python decoder/CPU work, model persistence or scheduling with a larger representative sample before implementing a trainer pool.
