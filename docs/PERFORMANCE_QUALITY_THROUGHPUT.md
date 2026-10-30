# Quality pipeline: bounded two-song throughput experiment

Hardware: Spark-WSL, RTX Spark N1X (64,000 MiB GPU-visible memory), 20 ARM CPU cores. Source: previously archived **original videos and confirmed lyrics**, using the same complete upstream quality pipeline from PR #80 and the same model weights. No song was added to a production queue. The serial and parallel runs have separate work directories.

| Song | Sequential complete pipeline | Two simultaneously running pipelines |
|---|---:|---:|
| BV1jv411w7q6 冻夏 | 380.891 s | 454.555 s |
| BV1gs411o7XG hello&bye,days | 427.129 s | 486.014 s |
| **Time to complete both** | **808.020 s** | **486.233 s** |
| **Time per completed song for this batch** | **404.010 s** | **243.117 s** |

The batch throughput ratio is **1.66×** (+66% songs/hour) with a **39.8% reduction** in batch completion time. This is not a single-song latency win: each song's individual wall time rises because concurrent Kim/Qwen work competes for GPU and CPU. The peak GPU reported used memory across the pair is **9,667 MiB**, far below the 64,000 MiB available. The original benchmark peak was around 4.9 GiB.

**Correctness check:** on both songs the 40/50 two-pass ASR windows match byte-for-byte in normalized text, language and bounds; both passes' TIFA native token timestamps match exactly. The final lyric, phoneme, syllable, timing and provenance data are identical after stripping only generated record IDs. Each output MP4 was generated successfully. The estimated-line counts were unchanged (0/28 and 5/36). Production remained on its existing commit and did not run this parallel code.

**Mechanism:** opt-in `VC_KTV_PREP_WORKERS=2` launches two bounded AutoPrep threads; independent per-song CUDA subprocesses and output directories can overlap. DB task claims, active-count/busy flags and final results are guarded by the event store lock, including closure/cancel paths. This does **not** alter Kim, two ASR passes, all nonempty ASR window TIFA, lyrics, block planning, seam repair or encoding algorithms. The default stays 1 until specifically approved for deployment.

**Raw data:** `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr80-archive-three-bv/` (serial) and `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr81-concurrent-two/` (parallel). The second contains `summary.json`, runner, full worker stderr/logs, MP4 outputs, full TIFA intermediate states and GPU samples. This measures only two songs; performance under three or more concurrent jobs is not established, nor have changes been tested in production.

## Rejected ASR batch-size shortcut

An additional isolated Qwen3-ASR experiment used the **same original vocal audio, verification passes and 40 windows** as 冻夏, but configured the vendor API's `asr_batch_size=2`. The Qwen worker completed in **56.212 s** versus **83.766 s** with batch size 1. **Five of the 40 window texts differed**, so this is **not** a correctness-preserving optimization under our current evidence requirement and is **not enabled** by this PR. We also deliberately did not replace Qwen inference with vLLM until exact ASR parity can be established. Raw comparison and model logs: `~/.local/share/vc-ktv/pipeline/benchmarks/20261030-pr81-qwen-batch2/`.
