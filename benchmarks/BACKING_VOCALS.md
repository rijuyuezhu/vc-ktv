# 伴唱音轨 POC

目标是 **保留乐器和和声、尽可能去除主唱**，而不是删除所有人声。本实验不改变点歌、视频或生产媒体。

## 两条路线

- **Direct**：用 Karaoke（lead / other）模型直接处理完整歌曲，以 `other` 作为候选输出（目标是乐器 + 和声；实际声部语义必须验证）。
- **Two-stage**：用普通 vocals / instrumental 模型先分离全部人声，再用 Karaoke 模型将人声分成两个候选声部；初版假定 primary 为 lead，把 `instrumental + residual` 混合。该假定有待验证，部分 Karaoke 模型可能恰好将 primary 定义为伴唱。合成时保留相对响度，只有发生削波风险时才整体衰减。

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

每条路线生成 `backing.m4a`（AAC 192 kbps）、`report.json`、分离出的 WAV 音轨。报告记录模型、生成耗时、文件大小、音频长度差、混音增益，以及模型目标声部 RMS。成品只是待验证候选，不代表通过伴唱音质验收。**这些指标不能代替人工试听**：需分别检查主唱残留、和声保留、乐器损伤以及声相位。使用相同音频片段和硬件运行两个模式才有可比性。

当前 POC 将提取的音频视为从 0 开始的时间轴；与视频 PTS、HTML 字幕和多媒体元素的最终同步由后续播放器 PR 验证。不得仅凭片段长度一致就认定音画已同步。分离模型权重的使用条款须在接入正式流程前确认。

不需要 GPU 或第三方模型即可执行合成音频测试：

```sh
uv run --no-sync pytest -q tests/test_backing_vocals_poc.py
```


## 初次真实实验（2026-11-17）

使用 CUDA GPU，对同一段 20 秒音乐进行模型推理；模型权重通过 SHA-256 核验，媒体与权重均保存在临时目录。两条路线均生成了可解码的 AAC 192 kbps，时长与输入一致（偏差 0 ms）。

| 指标 | Direct | Two-stage |
| --- | ---: | ---: |
| 首次完整耗时 | 39.85 s | 32.13 s |
| 预热后两次耗时 | 16.30 / 13.77 s | 23.99 / 23.83 s |
| AAC 成品体积（20s） | 493,847 B | 495,301 B |

**实际声部分离尚未达到验收要求。** 在真人演唱片段，已有 Kim 模型能提取非零人声（RMS 0.06972），但本次两款 RoFormer 的模型目标声部 RMS 均低于实际 WAV 输出的有效精度，文件全零，候选成品基本等于原音频。换用另一段歌曲及合成语音对照后仍未产生有效目标声部。时间与文件结果仅证明调用和产出路径可运行，**不能证明去主唱已生效，更不能用于判断和声保留程度**。

待解决：明确每个 Karaoke 模型的 primary / residual 对应哪种音乐声部；排查当前音频分离推理与 CUDA 环境适配差异；使用确认含主唱与和声的样本试听验证。已验证现有 Kim 能产生非零人声，因此可以作为后续两级方案的一级基线，但仅凭 Kim 不能区分主唱与和声。PR 在完成真实音质验证前保持 Draft，不进入生产 Pipeline。
