# 伴唱分离 POC

目标是保留乐器与和声，同时尽可能去掉主唱。视频、字幕和当前点歌流程不变。

## 方法

- **Direct**：Karaoke MelBand RoFormer 直接处理完整原音轨，取去掉目标主唱后的 residual。
- **Two-stage**：先通过现有 Kim MelBand 模型提取全部人声，再用 Karaoke 模型从人声中提取主唱，把人声 residual（和声候选）与乐器混合。只有最终 WAV 会按实际峰值防止削波，两个声部保持相对响度。

两条路线均复用项目可选 GPU 对齐环境中的 MelBandRoformer 及其 `demix_track` 推理实现。模型经严格权重加载，输出统一为 44.1 kHz 立体声；AAC 成品使用 192 kbps。没有额外安装第二套 RoFormer 推理框架。

## 本地实验

先运行项目的可选 GPU 资源安装，确认 Kim 的源码、Python CUDA 环境及 checkpoint 已齐备。另行准备 Karaoke checkpoint `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`。Karaoke 权重经 SHA-256 `1de20d459332fe8869aeb01327a31df0032262706e1365114e852dc271779813` 核验；现有 Kim checkpoint 的验证参见 `ktv_app/separation.py`。

```sh
# 仅供本地运行：模型和原始音频均不提交 Git。
# model-cache 中需放置：
#   MelBandRoformer.ckpt（可以链接到已安装的 Kim 权重）
#   mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt
uv run --no-sync python -m benchmarks.backing_vocals sample.mp4 \
  --mode both \
  --model-dir /path/to/model-cache \
  --model-source /path/to/Kim-source \
  --worker-python /path/to/separation-python \
  --output /tmp/vcktv-backing-results
```

输出 `direct/` 与 `two-stage/`，分别包括目标人声音轨、残余声部、可播放 `backing.m4a` 和 `report.json`。报告记录模型、时间、时长差、音量 RMS、成品大小和混音增益；`model-run.json` 还记录 CUDA 模型推理情况。候选音轨必须经过主唱残留、和声保留和乐器损伤的试听比较。

常规 CI 使用合成音频与模拟模型，无需下载权重或使用 GPU：

```sh
uv run --no-sync pytest -q tests/test_backing_vocals_poc.py
```

## 实测：2026-11-17，CUDA GPU

在一段 20 秒真人演唱音频上，相同 Karaoke 权重在 `audio-separator==0.48.0` 的推理路径输出接近全零的目标人声音轨；复用已验证的 Kim MelBand 实现、严格加载同一权重后，人声 RMS 为 **0.06919**，现有 Kim 对照为 **0.06972**。确认故障来自原 POC 的推理实现或其配置组合，而不是模型权重损坏。

| 指标 | Direct | Kim + Karaoke |
| --- | ---: | ---: |
| 20 秒样本完整耗时 | 18.61 s | 24.51 s |
| 模型目标人声 RMS | 0.06919 | 0.06946 |
| 目标人声 / 原混合音 RMS | 0.6972 | 0.6999 |
| AAC 192 kbps 大小 | 484,137 B | 484,155 B |
| 输出时长差 | 0 ms | 0 ms |

不同模型的首次加载和缓存预热会影响计时；更长歌曲需另外评估峰值显存与端到端吞吐。当前 POC 仅验证音频生成与候选声部分离：后续需要用包含明确和声的歌曲确认伴唱音质，以及在主屏中验证音视频播放时钟的同步。

输出中的原始和分离 WAV、AAC 与 checkpoint 均留在本地实验区。正式集成前还需核实第三方权重许可。

## 两首 Vocaloid 歌曲的补充实验（各 20 秒）

| 样本 | Direct 目标人声 RMS | Kim + Karaoke 目标人声 RMS | Direct 耗时 | Two-stage 耗时 |
| --- | ---: | ---: | ---: | ---: |
| 心华《冻夏》 | 0.08513 | 0.08499 | 13.70 s | 23.73 s |
| 洛天依《hello&bye,days》 | 0.14171 | 0.07975 | 14.09 s | 22.44 s |

两首歌的两种路线均输出准确的 20 秒音频。单次推理阶段的 PyTorch 显存分配峰值约 1.55 GiB（不含 CUDA/驱动的其他显存占用）；模型两阶段之间独立运行，不同时占用 GPU。

《冻夏》两种方案的目标人声结果非常接近（两种目标音轨相关系数约 0.998）。《hello&bye,days》差异较大（相关系数约 0.755）：Kim + Karaoke 在第二阶段得到 RMS 0.10401 的残余人声候选。**这些残余可能包括真正的和声，也可能有尚未去除的主唱**。因此虽然 Direct 更快，尚不能以速度代替和声保留的听感决策。

后续在正式接入点歌 Pipeline 前，需要对这两首歌的 `original.wav`、`backing.m4a` 及二级 `other.wav` 做 A/B 试听，确认主唱残留和和声保留。性能评估也需覆盖完整歌曲而非仅用短片段估计总耗时。
