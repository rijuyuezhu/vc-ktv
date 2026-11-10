# 系统架构

VC-KTV 是一个 FastAPI 服务，使用 SQLite 管理场次与队列，使用原生 JavaScript 提供点歌、管理和电视播放页面。媒体制作由后台 worker 完成。

## 主要组件

| 目录或模块 | 职责 |
| --- | --- |
| `vc_ktv/application/`、`api/`、`core/` | HTTP 接口、场次、SQLite 队列与播放状态 |
| `vc_ktv/sources/` | 来源接口及网易云、B站媒体实现 |
| `vc_ktv/lyrics/` | 搜索歌词和选择时间轴 |
| `vc_ktv/processing/` | 自动下载、制作、失败重试与任务取消 |
| `vc_ktv/rendering/`、`pipeline/`、`ktv_app/` | 字幕引擎适配；`ktv_app` 是固定版本的第三方源码 |
| `vc_ktv/infra/` | 配置、磁盘预算、媒体文件保护与进程取消 |
| `static/` | 点歌、管理员与电视播放器 |

## 一首歌如何准备

```text
点歌 → 解析来源 → 下载视频或音频 → 选择歌词
                                   ├─ 使用来源逐句时间轴
                                   └─ GPU 音频对齐（Kim / Qwen3-ASR / TIFA）
                                → 字幕及 MP4 → 播放队列 → 电视
```

B站新场次默认 1080p（支持 720p、480p 回退），也可手动选择清晰度，清晰度不足时回退。原片可保持现有策略（HEVC 转 H.264）、统一 H.264 或统一 AV1；AV1 优先取原生流，否则使用 NVENC 或 SVT-AV1 转码。网易云用音频与封面合成 720p 视频。两种来源共用歌词、字幕和队列，烧录成片仍为 H.264；电视 HTML 字幕可使用原片或 AV1/H.264 低码率缓存。

HTTP 请求和后台 Worker 使用同一场次的 SQLite 锁，切换场次时等待正在执行的操作结束。较大的队列状态 JSON 在客户端支持时使用 gzip 传输，视频及媒体 Range 响应保持原样。

## 数据

```text
~/.config/vc-ktv/               # 管理员密码、服务密钥
~/.local/share/vc-ktv/
  active-session                 # 当前场次
  sessions/<event-id>/
    info.json
    room.sqlite3                # 歌曲、用户、队列、设置
    media/                      # 原片、成片与制作中间文件
  .private/                      # 网易云登录态
  pipeline/                      # 可选 GPU 权重与运行时
```

场次数据相互独立，每场媒体与中间产物共享 200 GiB 磁盘预算。模型和运行时安装在单独目录，不存入 Git 仓库。字幕引擎的上游来源与许可见 [第三方说明](../third_party/autoassrenderer/README.md)。
