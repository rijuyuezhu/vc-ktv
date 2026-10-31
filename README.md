# VC KTV

基于 B 站 BV 号的自托管 KTV 点歌系统。使用 Python、FastAPI、SQLite 和静态网页，不需要 Docker、Node.js 或云端 LLM。适用于可信局域网和 Tailscale，**不建议直接向公网开放**。

## 使用

1. 管理员创建场次，将六位加入码发给大家。
2. 用户进入首页，输入加入码后注册或登录本场账号，再粘贴 BV 号或 B 站视频链接点歌。
3. 在线歌词由独立来源陆续提供候选（VCPedia、网易云音乐、LRCLIB），用户自行选择，也可上传 UTF-8 LRC（重新对齐时间戳），后台下载原视频并制作带字幕的视频。歌曲可以点开查看详情、下载或取消自己的待播歌曲（每人同时最多保留 10 首）；本场已唱歌曲也可查看和下载。
4. 管理员在管理首页查看队列、控制播放、下载当前视频；从「设置」管理场次与字幕选项，从「演唱统计」查看本场记录。管理员可重试字幕、把已可播放的待播歌曲指定为下一首，或安排已唱歌曲下一首重唱。新的下一首安排会替换旧的优先标记，不会打断当前播放。

公平轮播是默认队列策略：只看已唱首数，不比较歌曲时长；同一人最多获得两首的落后补偿，人数增多仍按轮次轮流。只有领取普通待播歌曲时才持久化调度补偿；未播放就取消不计数，普通歌曲领取后跳过仅消耗虚拟轮次。管理员置顶及重唱视为额外管理员轮次，不增加任何用户的演唱统计或公平计数；也可以切换为先来先唱。

默认下载不高于 720p 的原片，可在设置中改为 480p。字幕默认采用 **Kim 人声分离 + 双遍 Qwen3-ASR 定位 + 分块 TIFA 对齐与接缝修复**。保留 PR #62 拟声词读音映射，字幕仍显示原始歌词。成片保留源视频的音轨，默认**不进行约 30 MB 二次压缩**，可由管理员选择开启。自动对齐属于 best effort：不因切块时长超标而拒绝制作，模型对齐失败会烧录有明确标记的估计字幕；歌词和时间仍可能有误。

## 安装

需要 Linux、Python 3.12、uv、FFmpeg（含 libass 和 libx264）及 Noto CJK 字体。模型和 CUDA 环境不随仓库分发，未安装时可下载原视频，是否允许无字幕播放由管理员决定。

```bash
sudo apt-get install ffmpeg fonts-noto-cjk
git clone "$REPOSITORY_URL" vc-ktv
cd vc-ktv
uv sync --locked --group dev
./scripts/setup.sh
uv run python -m vc_ktv
```

点歌首页：`http://127.0.0.1:8778/`，管理页面：`http://127.0.0.1:8778/admin/`。配置文件保存在 `~/.config/vc-ktv`，本场数据库和媒体默认在 `~/.local/share/vc-ktv`。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `VC_KTV_HOST` | `127.0.0.1` | 监听地址 |
| `VC_KTV_PORT` | `8778` | 监听端口 |
| `VC_KTV_CONFIG` | `~/.config/vc-ktv` | 私密配置目录 |
| `VC_KTV_DATA` | `~/.local/share/vc-ktv` | 场次数据库及媒体 |
| `VC_KTV_AUTOPREPARE` | `1` | 自动准备任务开关 |
| `VC_KTV_PREP_WORKERS` | `1` | 同时制作的歌曲数，可设置 `1`–`4`，每首立即独立领取，不等待凑满并发数；Spark 实测推荐 `4` 并配合 Kim 单路调度 |

开发校验：`uv run pre-commit run --all-files`、`uv run pytest -q`。服务采用现有 `main` 的静态前端，不需要单独构建前端。

## 文档

- [场次与数据管理](docs/EVENTS.md)
- [字幕流水线与回退](docs/AUTOPREP.md)
- [本地模型与部署](docs/SPARK_WSL_PIPELINE.md)
- [开发约定](docs/DEVELOPMENT.md)
- [安全边界](docs/SECURITY.md)
- [4 Mbps 受限网络的码率实验](docs/PERFORMANCE_4MBPS.md)
- [可选来源逐句 LRC 与偏移校准](docs/PROVIDER_LRC.md)

本项目是私人测试原型，不提供公网投屏服务或原生乐播 SDK。视频和歌词版权属于各权利人，请仅处理有权使用的素材。音频分离、对齐及字幕渲染代码基于 [AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK) 修改，固定来源、Apache-2.0 许可及归属见 [第三方说明](third_party/autoassrenderer/README.md)；模型也适用各自条款。本仓库目前未授予对外开源许可。
