# 系统架构

VC-KTV 是一个 FastAPI 服务，使用 SQLite 管理场次与队列，使用原生 JavaScript 提供点歌、管理和电视播放页面。媒体制作由后台 worker 完成。

## 主要组件

| 目录或模块 | 职责 |
| --- | --- |
| `vc_ktv/application/`、`api/`、`core/` | HTTP 接口、场次、SQLite 队列与播放状态 |
| `vc_ktv/sources/` | 来源接口及网易云、B站媒体实现 |
| `vc_ktv/lyrics/` | 搜索歌词和选择时间轴 |
| `vc_ktv/processing/` | 媒体下载、字幕与伴奏制作、增量更新 |
| `vc_ktv/rendering/`、`pipeline/`、`ktv_app/` | 字幕引擎适配；`ktv_app` 是固定版本的第三方源码 |
| `vc_ktv/infra/` | 配置、磁盘预算、媒体文件保护与进程取消 |
| `static/` | 点歌、管理员与电视播放器 |

## 一首歌如何准备

```text
点歌 → 来源解析 → 原片
                    ├─ 来源时间轴 / GPU 对齐 → 字幕时间轴 → 可选烧录成片
                    └─ 后台伴奏生成（可选）
                 → 播放队列 → 网页播放器
```

B站优先获取符合设置的原生编码与清晰度，必要时转码；网易云由音频和封面合成视频。主屏 HTML 模式默认只发布字幕时间轴及符合码率预算的播放媒体，无需烧录；非 HTML 模式或开启额外烧录时生成 H.264 成片。HTML-only 歌曲在必需电视缓存就绪后才发布，烧录版缺失时不能静默播放无字幕原片。

普通、Fast 和伴奏 Worker 共用预计播放顺序，按各自能力领取最靠前的可执行任务；未制作的歌曲也参与预测，已开始的任务不抢占。独立的伴奏 Worker 在原片就绪后即可并行生成伴奏。媒体属性和制作配置用于判断待播歌曲是否需要更新；更新复用符合要求的原片、字幕时间轴和伴奏，完成后切换媒体引用。

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
    backing/                    # 可选伴奏缓存
  .private/                      # 网易云登录态
  pipeline/                      # 可选 GPU 权重与运行时
```

场次数据相互独立，每场媒体与中间产物共享 200 GiB 磁盘预算。模型和运行时安装在单独目录，不存入 Git 仓库。字幕引擎的上游来源与许可见 [第三方说明](../third_party/autoassrenderer/README.md)。
