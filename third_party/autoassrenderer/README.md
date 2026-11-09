# AutoASSrenderer4karaOK 来源说明

- 上游项目：[Volta233/AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK)
- 固定版本：`39cedb317c9e11add83e9a72c31368cabd8416d4`
- 源码许可：[Apache License 2.0](LICENSE)
- 来源记录：[UPSTREAM.json](UPSTREAM.json)
- 模型资源版本：[assets.lock.json](assets.lock.json)

VC-KTV 将上游音频分离、对齐和字幕生成模块适配到 `ktv_app/`，由 `vc_ktv/pipeline/adapter.py` 调用。`vc_ktv/` 实现场次、队列、媒体及网页服务。部分模型与字幕模块为适配 Linux/CUDA 和并行处理进行了修改。

上游代码、VC-KTV 适配代码以及模型权重分别遵循各自许可。升级上游时，同步审查 `UPSTREAM.json`、本地适配和模型资源锁文件。

仓库的 [upstream-check 工作流](../../.github/workflows/upstream-check.yml) 会检查上游是否更新；它只负责通知，不会自动替换源码或模型。
