# TIFA 声学对齐：现状与复现

**注意：以下主要是早期 TIFA 实验与管理员手工导入 TextGrid 的历史说明。自动字幕流程已切换为 [UPSTREAM_PIPELINE.md](UPSTREAM_PIPELINE.md) 的 Kim / Qwen3-ASR / TIFA 集成，Spark-WSL 实验见 [SPARK_WSL_PIPELINE.md](SPARK_WSL_PIPELINE.md)。**

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

自动流水线保留用户确认的原始歌词于每次制作的 `lyrics.source.txt`，另在 `audio.txt` 中记录送入 TIFA 的发音文本。`pronunciation_trace.json` 标明常见拟声词的替换及原文字段范围。TextGrid 的原生时间区间按源文字段映射回字幕；如果 TIFA 将一个拟声词分为多个音节，只使用这些区间的实际首尾时间并显示原有拟声词，不制造逐字母时间。未知拉丁字母唱词在进入 TIFA 前按长度转换成可识别的中文音节，并保留原文映射；不依赖可选英文 G2P 运行时。发音替换本身不是实际歌唱内容或时间精度的证明，需在授权真实样本上复核。

## Kim + 整首 TIFA 试听模式（Issue #59，2026-10-30）

默认 `VC_KTV_PIPELINE_MODE=global-kim`：原视频提取音频后，用 Kim 做人声分离（**只用于对齐**，导出的 MP4 仍保留原始音轨），再对完整歌词运行一次 TIFA，不经过 Qwen3-ASR、不分块；若 TIFA 无法运行，则允许单独标识的保底时间估算。继续使用早期 `pronunciation_lyrics` 发音映射及 `convert_textgrid` 全量文本/顺序/重叠校验；字幕样式、画质和成片体积控制使用现有代码。

保留 `global`（混合音频整首 TIFA）、`quality` 和 `fast` 用于离线 A/B。全曲对齐失败时，`global-kim` 内部允许改用原始混合音频重试同一算法，保留回退原因，不擅自切换到旧 Fast/Quality pipeline。 试听模式下只对过短/过长的原始时间戳生成 `suspicious_timing` 诊断，不篡改时间，不由于阈值拒绝整首输出；字幕时间仍需试听判断。每次尝试保留整个 TIFA `audio.TextGrid`、`tifa.log`、完整原歌词、读音映射、严格 LRC 和导出报告。

### 实歌对照（隔离实验，非客观准确率验证）

《霜雪千年》241 秒，34 行/480 字：直接整首 TIFA 对齐约 13.4 秒，第 1 行起点 2.08 秒、第 19 行 43.53 秒；Kim + 整首 TIFA 约 87 秒（包括约 57 秒分离），第 1 行起点 14.90 秒、第 19 行 6.28 秒，但第 22 行仍接近 30 秒、尾段有短句。两种模式均不能仅根据 `TextGrid` 的完整性断言音画准确。A/B 原始数据见 Spark-WSL `~/.local/share/vc-ktv-pipeline-staging/benchmarks/20261030-global-tifa-regression/`。生产试听时须用「重试字幕」重新对齐已完成的歌曲，已生成的旧成片不会自动改动。

### 自动对齐 best effort（Issue #61）

TIFA 默认会略去 zero-width 音素，导致整首歌词偶尔缺少一个或多个字。自动制作在 strict TextGrid 验证失败且属于文字不匹配时，保留原始 `audio.TextGrid`，用确认歌词与 TIFA 输出的有序文本进行局部对照，生成独立 `repaired.TextGrid` 并再次严格验证文字、时长和序列，保留 `best_effort` 差异诊断。缺字依附相邻真实模型时间区间，而不重新推理或重排整首歌；仍继续制作字幕视频。人工导入 TextGrid 的严格校验不改变。不能把缺字当成整首失败；有疑点的字幕可继续试听校正。

针对旧场次 `BV1yVQFBNEPn`《太阳是什么颜色》进行隔离实机回归（2026-10-30）：44 行歌词，9 处 `DUDUDU`/`DUDUDUDU`/`DaLaDa` 发音映射全部保留，Kim+整首 TIFA 约 71.7 秒成功导出全部 44 行时间轴。原生 500 个规范化字中漏了「诺」「恣」2 字，best-effort 仅修补 2 个区间。第 34/36 行有 0.38/31.56 秒时长异常，说明声学精度仍需试听；**但不再将该异常或局部文字遗漏升级为整首字幕失败**。原始音频、TextGrid、修复版和报告保存在 `~/.local/share/vc-ktv-pipeline-staging/benchmarks/20261030-best-effort-scat-BV1yVQFBNEPn/`。

### 自动字幕可用性兜底（Issue #61 扩展）

对生产 `global-kim` 和 `global` 模式贯彻 best effort：首先尝试 Kim（如果选中）和整首 TIFA；Kim 不可用则以原混合音频直接执行 TIFA；文本少量缺漏则利用原始 TextGrid 锚点进行单字/局部修复，并把受影响单元标记为 `estimated`；在 **TIFA 之前** 将已知重复拟声词（如 BIBIBI、BAPABAPA、DUDUDU、DaLaDa）转成中文音节，并把仍未知的拉丁字母唱词以通用“啦”音节近似，**字幕始终保留原文**。TIFA 运行使用 `--skip-handling preserve`，避免它默认 `omit` 丢掉零时长音素，自动生成 LRC 保留毫秒精度；若完整人声版 TIFA 仍失败，再尝试用混合音频整首对齐。若 TIFA 的全部尝试均失败，则估计人声音量区间，将完整歌词按顺序、长度权重分配到粗时间轴，继续渲染视频。粗时间轴所有单元标记为 `estimated`，报告的 `alignment_quality` 设置为 `rough`；仅局部修复为 `repaired`，使用通用拟声词的 TIFA 为 `phonetic_approx`，正常完整 TIFA 为 `model`。错误和回退步骤保存 `fallback_report.json`，原片、原始模型输出、诊断文件都保留。

这不是声学准确率保证，尤其整首歌词均匀估算的粗时间轴仅能保证字幕存在、文字完整、顺序正确，无法保证对应演唱秒数。在可播放原视频和确认歌词有效的前提下，优先交付能试听的带字幕 MP4，而不是因不认识拟声词或某个字的漏识别直接宣布失败。人工上传 TextGrid 的严格验证仍不改变。

### TIFA 可配置的容错参数及实机验证（2026-10-30）

TIFA 默认 `--skip-handling omit` 会丢弃零时长音素；`--skip-handling preserve` 给这些音素分配约 1ms 区间而不删除文字。原先《星花挽愿》588 字只导出 587 字；同一个音频用 `preserve` 再跑一遍得到 588 字，自动 TextGrid→LRC 使用毫秒精度后，40 行歌词全部通过，无需文字修复。百分秒格式的老导出会将 1–2ms 的相邻区间舍入成同一时间，所以保留三位小数只用于自动流程，人工导入格式不受影响。

`--oov-handling force` 只会丢弃不支持的**发音候选**，并不是“忽略生词而保留其时间”的能力。模型词表包含 `zh/` 和 `en/` 音素，但要启用英文 G2P 的 `-L en` 时，实际实验被缺少 `onnxruntime` 阻断。优先做已知拟声词精确转换与未知拉丁字母的近似中文读音映射，无需在生产加入新的模型运行时依赖。原文始终用于最终展示，转换仅用于 TIFA 的文字输入。此机制依旧不能保证声学毫秒准确，全部近似和回退均通过诊断记录。
