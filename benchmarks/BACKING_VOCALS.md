# 伴奏分离基准

目标是保留乐器与和声，同时尽可能去掉主唱。视频、字幕和当前点歌流程不变。

## 方法

- **Direct**：Karaoke MelBand RoFormer 直接处理完整原音轨，取去掉目标主唱后的 residual。
- **Two-stage**：先通过现有 Kim MelBand 模型提取全部人声，再用 Karaoke 模型从人声中提取主唱，把人声 residual（和声候选）与乐器混合。两种输出均使用统一的峰值保护，两个声部保持相对响度。

两条路线均复用项目可选 GPU 对齐环境中的 MelBandRoformer 及其 `demix_track` 推理实现。模型经严格权重加载，输出统一为 44.1 kHz 立体声；AAC 成品使用 192 kbps。没有额外安装第二套 RoFormer 推理框架。

## 本地实验

按 [安装指南](../docs/INSTALL.md) 安装 GPU 模型和运行时。基准使用 Kim 与 Karaoke checkpoint，分别比较主唱残留、和声保留和生成耗时。

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
