# GPU 字幕运行与验收（Linux CUDA）

本页记录已在 **Linux ARM64 / WSL、Python 3.12、CUDA 13.0** 验证的 Kim、Qwen3-ASR 和 TIFA 环境。其它 Linux 平台可使用相同安装方式，但需独立验证 GPU 与模型推理。

## 安装

按照 [安装指南](INSTALL.md) 依次准备模型资源、独立 CUDA PyTorch 环境及 TIFA/Kim/Qwen 运行时。不要直接依赖其它项目已有的 Python 环境。

模型默认位于 `~/.local/share/vc-ktv/pipeline/`，可通过 `VC_KTV_PIPELINE_ROOT` 自定义。源码和权重版本见 [UPSTREAM.json](../third_party/autoassrenderer/UPSTREAM.json) 与 [assets.lock.json](../third_party/autoassrenderer/assets.lock.json)。安装完成后先检查：

```sh
uv run --no-sync python -m scripts.check_install --mode gpu
```

## 验证实际推理

安装预检只检查必要文件与工具。可以使用约 8 秒合成语音运行完整的 Kim → Qwen3-ASR → TIFA → ASS/MP4 流水线：

```sh
uv run --no-sync python -m scripts.gpu_smoke
```

该测试另需 `espeak-ng`、FFmpeg/libx264/libass；会在隔离临时目录运行，验证原生模型对齐、歌词覆盖及音轨保持。详见 [验证范围](VALIDATION.md)。

也可以用自己有权处理的素材验证完整歌曲：

```sh
uv run --no-sync python -m scripts.run_spark_pipeline \
  --source /absolute/path/song.mp4 \
  --lyrics /absolute/path/lyrics.txt \
  --out /absolute/path/new-attempt
```

第二条命令会保留每阶段时间轴、日志、ASS/LRC 和成片；输出目录应为新的独立目录。历史短样例成功不代表其它翻唱、长间奏或并发任务必然准确。生产设置及部署步骤仍以 [安装指南](INSTALL.md) 和 [升级与回滚](RELEASE.md) 为准。
