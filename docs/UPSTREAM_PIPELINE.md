# 上游字幕引擎

VC KTV 复用并修改 [AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK) 的模型调用、字幕生成和渲染模块。固定上游版本为 `39cedb317c9e11add83e9a72c31368cabd8416d4`，许可与修改记录见 [third_party/autoassrenderer](../third_party/autoassrenderer/README.md)。请保留该目录及其归属、许可证文件。

`ktv_app/` 存放改造的上游核心代码，`vc_ktv/upstream_pipeline.py` 是 VC KTV 的调用边界。场次、权限、队列、媒体文件持久化仍由 `vc_ktv/` 管理，不能让上游引擎直接修改活动数据库。

默认 `quality` 模式通过 Kim、双遍 Qwen3-ASR 与完整窗口 TIFA 定位后，对原歌词分块对齐并修复接缝；`global-kim` 和 `fast` 仅保留为可选对比模式。具体策略见 [AUTOPREP.md](AUTOPREP.md)。模型权重及 CUDA 环境不在仓库中，完整运行需配置 [Spark-WSL](SPARK_WSL_PIPELINE.md)。

`tests/upstream/` 主要测试字幕时间轴、归属和多种模型回退边界；这些测试无需 GPU。真实长歌曲、音视频一致性和设备播放仍需实机验收。
