# TIFA 声学对齐：现状与复现

> **版本说明**：本页主体记录 v0.3.1 时的 TIFA 验收数据，并非当前程序的运行能力全表。v0.5.0 已增加用户选词后自动触发本地 TIFA/FFmpeg 的后台流程，模型和 GPU 环境仍需管理员在部署前配置一次。未安装模型时会记录失败；当前默认字幕门槛阻止原片进入可播队列。

## 已完成的验证

在一台配有 NVIDIA GPU 的 ARM64 Linux 主机上，官方 TIFA-1.0-ST 已成功完成 **CUDA 实际音频推理** 并输出 TextGrid。VC KTV 已能将其 texts tier 与人工核对的歌词严格匹配，转换成增强 LRC，并利用 FFmpeg/libass 烧录成 MP4。

从 v0.4 起，后台可自动调用本地 TIFA 并尽力生成成片；失败时保留原视频；只有管理员开启允许无字幕时才可播放。这不代表实际歌声对齐准确率已验证。

## 源码、模型与独立环境

- TIFA 上游：https://github.com/openvpi/TIFA；固定源码提交 614a277d2580efe5e4b84faf4c34753dbc5a062c。
- Git 忽略目录：.runtime/TIFA。
- 官方 TIFA-1.0-ST 权重：.runtime/models/TIFA-1.0-ST/model.pt。官方压缩包大小 150,789,526 bytes；SHA-256 为 6da6832cd2cb981aae1ccc2d9330f8f7c085ce7977d1eb13bff63531d038a88b。
- 解压后的 model.pt 实测 SHA-256 为 8d9bf896da1827f36bf135f80c7ab1fc4f3c1d48f328818b8402c9527c0f014b。官方 ZIP 经 SHA-256 校验并解压后，已清理其重复本地副本以节约空间。
- Python 3.12 环境：.runtime/tifa-env；TIFA 直接及间接依赖单独安装。为免重复下载 CUDA wheel，在此虚拟环境中配置了只读路径引用，复用 本机既有环境中的 PyTorch（与应用环境隔离）。研究环境本身没有更改，但实验环境仍依赖该外部路径，不是严格自包含，生产部署需重新独立打包 GPU 栈。
- 音频、权重、输出、诊断与日志全部在 Git 忽略的 .runtime 下；小型测试 fixture 位于 tests/fixtures。

## 管理员手动运行

准备自己有权使用的音频，以及文件名相同、内容和音频完全一致的 UTF-8 纯文本歌词，例如 audio.wav 和 audio.txt。

~~~bash
cd /path/to/vc-ktv
./scripts/tifa_align.sh /path/to/audio.wav /path/to/output/
~~~

脚本执行官方 infer.py，默认语言 zh，batch-size 1，num-workers 0，启用官方 --stat 诊断。可以设置 TIFA_LANGUAGE 环境变量更改语言。产物为 output/audio.TextGrid 和统计文件；审核后，在 VC KTV 管理员页面上传源视频并导入 TextGrid。接口 POST /api/admin/songs/{id}/alignment/import?offset=0 会检查歌词文本、词序和时间范围，不匹配即拒绝，不猜测补齐语义词或伪造逐字时间。

## 两次实际 CUDA 测量

| 项目 | 结果 |
| --- | --- |
| 样本 | espeak-ng 自行合成的普通话「你好世界」，2.263628 秒；不是歌声 |
| 设备 | ARM64 Linux + NVIDIA CUDA GPU（实验环境） |
| 模型 | ForcedAlignmentModel，39,604,224 参数 |
| 第一次进程完整耗时（包含 --stat） | 45.84 秒 |
| 第二次进程完整耗时（不含 --stat） | 15.44 秒 |
| 第三次脚本复跑（包含 --stat） | 28.66 秒，TextGrid 与前两次相同 |
| 第二次进程最大 RSS | 2,757,452 KiB |
| GPU 总占用 500ms 采样峰值 | 1257 MiB；监测时基线约 821 MiB |
| 输出 | 4 个 texts 区间，同时提供 words、phones tier；两次 TextGrid 完全相同 |
| 官方自检指标 | agreement 0.9999、confidence 0.6890、determinacy 0.9340、monotonicity 0.9705 |

上述耗时包括启动、模型初始化、预处理及推理。首次与第三次使用 --stat 并产出诊断图，第二次没有；因此三次不是完全相同的基准配置，不宜仅由第一次和第二次耗时差异推断冷启动成本。GPU 指标是整卡占用的离散采样，不等于模型自己的显存峰值。模型自检指标**不是人工标注的真值准确率**。本实验不能外推 3 分钟真实 VC 歌曲、复杂伴奏、多歌并发或实际偏移误差。

原始模型结果、日志与编码样本存放在 .runtime/tifa-samples/；选取真实模型生成的一份 hello.TextGrid 作为 tests/fixtures/tifa-espeak-hello.TextGrid，用于无需 GPU 的回归测试。

## 后续方向

1. 获取有使用权、与歌词版本一致的真实中文 VC 演唱片段，制作少量人工校正基准，计算 onset、offset 偏差及漏词率。
2. 比较带伴奏原音轨和人声分离音轨；TIFA 能运行不等于伴奏条件下对齐可靠。
3. 用真实样本估算整首歌的处理时间、缓存收益及并发 GPU 需求，然后决定是否值得接入后台自动任务。
4. 保留音频、模型配置、完整 TextGrid、官方诊断及人工审核轨迹；不把仅文字检索结果包装为准确时间轴。
5. Android 乐播 SDK 的真实接收端仍需独立验收；网页连播测试不可替代投屏设备验证。

不会自动下载 B 站媒体，或将未授权的真实音乐录音纳入实验数据。

## 歌词原文与 TIFA 发音提示

自动流水线保留用户确认的原始歌词于每次制作的 `lyrics.source.txt`，另在 `audio.txt` 中记录送入 TIFA 的发音文本。`pronunciation_trace.json` 标明常见拟声词的替换及原文字段范围。TextGrid 的原生时间区间按源文字段映射回字幕；如果 TIFA 将一个拟声词分为多个音节，只使用这些区间的实际首尾时间并显示原有拟声词，不制造逐字母时间。对于不支持的其他词仍记录 OOV 失败。发音替换本身不是实际歌唱内容或时间精度的证明，需在授权真实样本上复核。
