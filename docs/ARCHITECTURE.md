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

B站下载原视频，H.265 原片自动转为 H.264；管理员可选择将 AV1 等原片也统一转为 H.264。网易云用音频与专辑封面制作视频。两种来源共用场次、歌词、字幕、队列与播放流程。生成的视频可下载，也可以用电视页的 HTML 字幕模式播放。

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
