# 自动点歌与歌词制作流水线

文档描述 #29 的新 AutoPrep 自动制作链路；历史验收记录见 [VALIDATION.md](VALIDATION.md)，上游版本固定信息见 [UPSTREAM_PIPELINE.md](UPSTREAM_PIPELINE.md)。**生产服务升级须单独批准。**

## 用户与管理员的边界

普通用户：输入 BV 号 → 等待自动搜索 → 从候选列表选歌词，或者自行输入关键词搜索 → 返回点歌大厅。多个用户可以并行点歌，歌词选择只对原点歌者开放。用户不选歌词时保留原片，但默认字幕门槛会暂缓播放；管理员可选择允许无字幕播放。

管理员：登录并打开 TV 播放页，首次点击播放后自动切歌。用户选词、模型下载和字幕审核都不交给管理员。TV 目前采用浏览器或屏幕镜像，尚未接入原生乐播 SDK。

## 准备流程与回退策略

1. 提交 BV 时向 B 站公开接口查询视频标题、分 P 和时长，写入 SQLite，立即返回歌曲 ID。
2. 后台单线程按请求顺序下载原片，首选 yt-dlp；若其网页解析失败，尝试 B 站公开 view/playurl、选择 H.264/AAC DASH/MP4 并用 FFmpeg 无损封装。公开媒体获取失败后有限次重试（60/300 秒），不阻塞其他歌曲。
3. 视频完成 FFprobe 校验即进入 Ready；是否可进入 TV 队列由本场字幕门槛决定。
4. 前端根据标题提取歌名，先查 VCPedia 与 LRCLIB；候选不足或来源单一时尝试中日文字形、网易云音乐原文 LRC、UtaTen 与 J-Lyric 的固定来源正文，最多展示 6 条且优先覆盖不同来源。用户可自行编辑搜索词；不自动选择候选。
5. 点歌者选择候选后，后台使用已确认歌词运行上游完整流程：音频提取 → Kim 分离推理人声 → 双遍 Qwen3-ASR 找位置 → TIFA 安全分块/局部恢复 → 标注原生或估算时间 → ASS/LRC → MP4。歌词显示始终使用原文，成片保留原视频音轨；失败保留当前尝试日志与原片。
6. 只有歌曲仍为 Ready 且原片未改变时，才切换成带字幕成片；如果已经开始播放，则保留正在播放的原视频，不中途换流。
7. 未选歌词、歌词未搜到、TIFA 无法处理、转码失败时保留原视频文件，但默认不允许无字幕播放。管理员可在高级队列操作中仅重试失败的字幕制作，不重新下载；每次尝试的日志独立归档。

## 状态说明

- pending / downloading：原视频等待下载或下载中。
- unavailable：媒体暂无法取得，显示原因与重试次数；其他歌曲照常进行。
- downloaded：原片已就绪，尚未选择歌词或等待字幕处理。
- aligning：已选歌词，正在运行 Kim/Qwen3-ASR/TIFA/FFmpeg 制作；除非管理员允许无字幕，原片不可播。
- done：字幕成片已制作，尚未开始播放时可切换到成片。
- original：字幕生成失败，保留原片及诊断，默认不可播；可重试字幕。
- manual：管理员通过高级功能主动上传素材。

Ready/playing/played/skipped 是独立于准备阶段的播放状态；只有 Ready 才能进入公平可播队列。数据库和媒体都保存在 VC_KTV_DATA 下（默认 ~/.local/share/vc-ktv）。

## 验证范围与限制

- 在 Spark-WSL ARM64 CUDA 上，已验证实际 Kim → Qwen3-ASR → TIFA → ASS/MP4：4.73 秒 ACE-KiSing 歌声、逐字模型时间戳、原始音轨 AAC 数据一致。见 [SPARK_WSL_PIPELINE.md](SPARK_WSL_PIPELINE.md)。
- 针对一个公开测试视频，yt-dlp 页面解析失败后，公开 API 备用下载成功并生成 H.264/AAC MP4。
- 在隔离场次测试了访客点歌 → 选词 → AutoPrep → 真实模型 → done → TV next → 媒体端点 200；原音轨 MD5 一致。这里使用本地许可素材代替实际 B 站下载。CI 覆盖选词授权、后台任务、失败回退、字幕重试、场次隔离和上游算法边界；真实长歌和乐播设备仍需另验。
- 对齐的时间戳只是 best effort：相同歌词文本也可能与翻唱录音有不同的速度、跳句或乐器间奏；不能据此宣称准确率达标。

## 来源与媒体权利

VCPedia、LRCLIB 与 B 站各有独立的访问及版权条件。可获取内容不等于可以公开再分发。TIFA、Kim、Qwen3-ASR 均使用本地模型，权重不放入 Git。处理流水线已从 [AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK) 复制/适配，原作者许可见 [third_party/autoassrenderer](../third_party/autoassrenderer/README.md)，各模型另有独立条款；未复制上游的 GUI、Windows 安装器或数据库。
