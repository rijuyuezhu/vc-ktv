# 伴唱音轨 POC

目标是 **保留乐器和和声、尽可能去除主唱**，而不是删除所有人声。本实验不改变点歌、视频或生产媒体。

## 两条路线

- **Direct**：用 Karaoke（lead / other）模型直接处理完整歌曲，取 `other`（乐器 + 和声）。
- **Two-stage**：用普通 vocals / instrumental 模型先分离全部人声，再用同一 Karaoke 模型从人声中分出 lead / backing vocals；将 `instrumental + backing vocals` 混合。合成时保留相对响度，只有发生削波风险时才整体衰减。

默认候选是 [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) 支持的 `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt` 和 `model_bs_roformer_ep_317_sdr_12.9755.ckpt`。当前 Kim 人声模型只用于字幕对齐，不能直接提供“去主唱、留和声”音轨；本 POC 的 two-stage 首级是可复现的通用 vocals 模型，尚未验证改用 Kim 的效果。

## 本地运行

在独立 Python 3.12 环境安装 [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) 0.48.0。模型会在首次使用时下载；权重和源音频只保存在指定的本地目录，不放进 Git 仓库。

```sh
uv venv --python 3.12 /tmp/vcktv-separator
uv pip install --python /tmp/vcktv-separator/bin/python 'audio-separator==0.48.0'
/tmp/vcktv-separator/bin/python -m benchmarks.backing_vocals ./sample.mp4 \
  --output /tmp/vcktv-backing-results \
  --model-dir /tmp/vcktv-backing-models \
  --mode both
```

每条路线生成 `backing.m4a`（AAC 192 kbps）、`report.json`、分离出的 WAV 音轨。报告记录模型、生成耗时、文件大小、音频长度差与混音增益。**这些指标不能代替人工试听**：需分别检查主唱残留、和声保留、乐器损伤以及声相位。使用相同音频片段和硬件运行两个模式才有可比性。

当前 POC 将提取的音频视为从 0 开始的时间轴；与视频 PTS、HTML 字幕和多媒体元素的最终同步由后续播放器 PR 验证。不得仅凭片段长度一致就认定音画已同步。分离模型权重的使用条款须在接入正式流程前确认。

不需要 GPU 或第三方模型即可执行合成音频测试：

```sh
uv run --no-sync pytest -q tests/test_backing_vocals_poc.py
```
