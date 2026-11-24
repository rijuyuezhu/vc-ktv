# AutoASSrenderer4karaOK 来源说明

- 上游项目：[Volta233/AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK)
- 上游审查基准：`00aaa7f8a9c4b836b5b165e0413db4354c451859`（1.5.0）
- 初始适配来源：`39cedb317c9e11add83e9a72c31368cabd8416d4`（1.3.5）
- 源码许可：[Apache License 2.0](LICENSE)
- 来源记录：[UPSTREAM.json](UPSTREAM.json)
- 模型资源版本：[assets.lock.json](assets.lock.json)

VC-KTV 将上游音频分离、对齐和字幕生成模块适配到 `ktv_app/`，由 `vc_ktv/pipeline/adapter.py` 调用。`vc_ktv/` 实现场次、队列、媒体及网页服务。部分模型与字幕模块为适配 Linux/CUDA 和并行处理进行了修改。

上游审查基准记录已审查并按需吸收的版本，不表示整个上游应用或模型已同步。

## 模型与资源

- 人声分离：[Kim Mel-Band Roformer](https://github.com/KimberleyJensen/Mel-Band-Roformer-Vocal-Model)，Kimberley Jensen；模型架构见 [Mel-Band RoFormer for Music Source Separation](https://arxiv.org/abs/2310.01809)，Ju-Chiang Wang、Wei-Tsung Lu、Minz Won。
- 语音识别：[Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)，Qwen Team。
- 强制对齐：[TIFA](https://github.com/openvpi/TIFA) 与 [g2pflow](https://github.com/openvpi/g2pflow)，Team OpenVPI；日语词典 [UniDic](https://clrd.ninjal.ac.jp/unidic/)，国立国語研究所。

模型来源与署名参考上游 [ACKNOWLEDGMENTS.md](https://github.com/Volta233/AutoASSrenderer4karaOK/blob/00aaa7f8a9c4b836b5b165e0413db4354c451859/ACKNOWLEDGMENTS.md)。具体版本以本仓库资源锁文件为准。

上游代码、VC-KTV 适配代码以及模型权重分别遵循各自许可。升级上游时，同步审查 `UPSTREAM.json`、本地适配和模型资源锁文件。

仓库的 [upstream-check 工作流](../../.github/workflows/upstream-check.yml) 会检查上游是否更新；它只负责通知，不会自动替换源码或模型。
