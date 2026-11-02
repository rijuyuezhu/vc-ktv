# Spark-WSL 字幕模型环境

此页供需要独立运行 CUDA 模型的开发者使用，不是普通用户的安装步骤。模型权重与独立 Python 运行时不放入 Git。上游资源固定信息见 [third_party/autoassrenderer/UPSTREAM.json](../third_party/autoassrenderer/UPSTREAM.json) 和 [assets.lock.json](../third_party/autoassrenderer/assets.lock.json)。

该页记录已经验证过的 Linux ARM64 / WSL 流水线配置；其他环境首先参考 [通用安装说明](INSTALL.md) 并单独验证。模型与运行时默认放在 `~/.local/share/vc-ktv/pipeline/`，TIFA CUDA Python 环境必须独立准备，不能依赖仓库 `.runtime` 的软链接。`setup_spark_assets all` 现在包括 TIFA 源码、模型和字典，但不会创建 CUDA 运行时：

```sh
uv sync --locked --group dev
uv run --no-sync python -m scripts.setup_spark_assets all
# 在运行下面命令前，必须先独立准备 runtime/tifa/bin/python（CUDA PyTorch + TIFA）
uv run --no-sync python -m scripts.setup_spark_runtimes all
uv run --no-sync python -m scripts.check_install --mode gpu
```

Kim、Qwen3-ASR 模型和运行时直接安装在 `~/.local/share/vc-ktv/pipeline/`，不会自动修改活动场次。Kim、TIFA 和可选 Qwen3-ASR 使用独立运行环境，避免 PyTorch/CUDA 版本冲突；TIFA 的本地路径由环境配置决定。

用授权素材单独测试：

```sh
uv run --no-sync python -m scripts.run_spark_pipeline \
  --source /absolute/path/song.mp4 \
  --lyrics /absolute/path/lyrics.txt \
  --out /absolute/path/new-attempt
```

该测试会写出对齐时间轴、ASS/LRC、MP4 和尝试报告。恢复方案默认 `quality`，需要 Kim、Qwen3-ASR 和 TIFA。部署时必须检查实际运行进程的 `VC_KTV_PIPELINE_MODE`，不能依赖文档中的旧部署快照；本次修改不会调整当前服务。不要将短样例的成功或相同音轨校验视为长歌曲字幕准确率证明。旧实验过程与详细输出可从 Git 历史和本机隔离归档中查阅。
