# Spark-WSL 字幕模型环境

此页供需要独立运行 CUDA 模型的开发者使用，不是普通用户的安装步骤。模型权重与独立 Python 运行时不放入 Git。上游资源固定信息见 [third_party/autoassrenderer/UPSTREAM.json](../third_party/autoassrenderer/UPSTREAM.json) 和 [assets.lock.json](../third_party/autoassrenderer/assets.lock.json)。

Linux ARM64 环境中：

```sh
uv sync --locked --group dev
uv run --no-sync python -m scripts.setup_spark_assets all
uv run --no-sync python -m scripts.setup_spark_runtimes all
```

相关模型和缓存进入 `~/.local/share/vc-ktv-pipeline-staging/`，不会自动修改活动场次。Kim、TIFA 和可选 Qwen3-ASR 使用独立运行环境，避免 PyTorch/CUDA 版本冲突；TIFA 的本地路径由环境配置决定。

用授权素材单独测试：

```sh
uv run --no-sync python -m scripts.run_spark_pipeline \
  --source /absolute/path/song.mp4 \
  --lyrics /absolute/path/lyrics.txt \
  --out /absolute/path/new-attempt
```

该测试会写出对齐时间轴、ASS/LRC、MP4 和尝试报告。默认生产 `global-kim` 只需 Kim 与整首 TIFA；可选的 `quality` 模式还依赖 Qwen3-ASR。不要将短样例的成功或相同音轨校验视为长歌曲字幕准确率证明。旧实验过程与详细输出可从 Git 历史和本机隔离归档中查阅。
