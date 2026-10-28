# VC KTV

基于 B 站 BV 号的自托管 Vocaloid KTV 点歌系统。无需永久账户、Docker、Node.js 运行环境、云端 LLM 或 Agent；服务端使用 Python、FastAPI 和 SQLite。项目目前处于 **v0.5.0 原型 / 私有测试阶段**，尚未正式发布。

## 使用方式

1. **管理员**创建一场 KTV，得到六位加入码。参与者进入首页先输入该码，再用用户名和个人密码注册（确认两次），注册后自动登录；旧场账号不会延续到新场。
2. **点歌者**登录后在网页粘贴 BV 号或 B 站视频链接。系统自动读取歌曲信息并准备公开可获取的视频。
2. 系统根据歌名搜索 **VCPedia / LRCLIB / 网易云音乐 / UtaTen / J-Lyric**，列出多个歌词候选，附来源、摘要、完整歌词。**由点歌者**选定演唱版本；没有满意选项可以自行修改关键词再搜。
3. 若选定歌词且本机已配置 Kim、Qwen3-ASR 和 TIFA，后台会按上游分块对齐流水线制作字幕，再通过 FFmpeg/libass 烧录 KTV 视频。默认只有字幕烧录完成的歌曲进入可播放队列；管理员也可在「本场设置」中允许播放未嵌字幕的原视频，并选择“经典双行／大屏高对比／简洁单行”字幕预设；已完成歌曲可由管理员主动按新风格重制。手动上传的成品视频可直接排队。
4. **管理员**登录一次，打开电视播放页并首次点击播放。之后大屏播放器会自动接续队列、回报播放完成，无需逐首手动操作。

管理员可创建、结束和归档 KTV 场次。每场独立存放歌曲、演唱统计、SQLite 和媒体文件；旧版本数据会先保持在原目录，不会自动搬移。具体见 [场次与数据管理](docs/EVENTS.md)。新场采用六位加入码与临时用户登录；用户和密码哈希只保存在本场 SQLite，场次结束即归档。

支持多人点歌、公平队列、管理入口、进度状态和失败重试。视频下载优先使用 yt-dlp；解析失败时尝试 B 站公开播放接口。仅支持公开可访问的流，不绕过平台访问限制。

> **投屏现状**：目前已实现 Web TV 自动连播，可在电视浏览器使用，或将浏览器画面镜像到电视。**没有接入 Android 乐播 Sender SDK，也未验收包厢接收设备**。不能将 Web TV 模式视为乐播直连。

## 本地启动

推荐 Linux / Python **3.12**，要求 FFmpeg（包含 libass、libx264）、Noto CJK 字体及 uv。没有 GPU 或模型时仍可下载原视频；管理员可按场次允许原视频直接播放。

~~~bash
# Ubuntu：安装系统依赖
sudo apt-get install ffmpeg fonts-noto-cjk

# 请先设置 REPOSITORY_URL 为你有权访问的仓库地址
git clone "$REPOSITORY_URL" vc-ktv
cd vc-ktv
uv sync --locked --group dev

# 仅在首次启动时创建本机管理员密码及签名密钥
./scripts/setup.sh

# 默认仅监听本机 127.0.0.1:8778
uv run python -m vc_ktv
~~~

点歌首页为 http://127.0.0.1:8778/ ，管理员控制台为 http://127.0.0.1:8778/admin/ （可直接刷新并保留页面位置）。管理员密码保存在 **~/.config/vc-ktv/admin-password**；签名密钥保存在 **~/.config/vc-ktv/token-secret**。请勿提交、分享或复制到代码仓库。

可使用环境变量设置运行参数：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| VC_KTV_HOST | 127.0.0.1 | 监听地址；需要其他设备访问时显式指定可信接口 |
| VC_KTV_PORT | 8778 | HTTP 端口 |
| VC_KTV_CONFIG | ~/.config/vc-ktv | 本机私密配置 |
| VC_KTV_DATA | ~/.local/share/vc-ktv | SQLite 数据库与媒体文件 |
| VC_KTV_AUTOPREPARE | 1 | 是否启动自动准备线程；0 用于测试/排障 |

**注意**：新克隆的仓库不包含 TIFA、Kim、Qwen3-ASR 的模型权重、CUDA 或 PyTorch 环境。它们是独立的**运行资源**，不属于 uv 的依赖；Spark-WSL 安装和端到端验证见 [模型环境](docs/SPARK_WSL_PIPELINE.md)。未安装时字幕制作失败会保留原片，是否允许原片播放由管理员设置。字幕流程为**本地机器学习推理**，虽然没有云端 AI 服务/Agent 参与，仍可能出现错字、错位、音节遗漏。

## 开发与校验

~~~bash
uv sync --locked --group dev
uv run pre-commit install
uv run pre-commit run --all-files
uv run pytest -q
bash -n scripts/*.sh
~~~

GitHub Actions CI 在推送到 main / PR 时运行 pre-commit、pytest、Shell 与 JavaScript 语法检查。CI 不会下载 B 站视频或 TIFA 权重，也不会使用管理员密钥；全流程媒体与 CUDA 测试属于单独的实机验收。

- [开发与贡献约定](docs/DEVELOPMENT.md)
- [自动点歌与字幕流水线](docs/AUTOPREP.md)
- [与上游功能的取舍和规划](docs/UPSTREAM_SCOPE_DECISIONS.md)
- [场次设置、归档与 BV 点歌记录导出](docs/EVENTS.md)
- [TIFA 环境与实验](docs/ALIGNMENT.md)
- [验收记录（按版本归档）](docs/VALIDATION.md)
- [安全与部署边界](docs/SECURITY.md)

核心目录：**vc_ktv/** 为后端；**static/** 为点歌/管理员/电视前端；**scripts/** 为初始化及 TIFA 调试；**tests/** 为可离线运行的自动化测试。生产媒体和模型不纳入版本控制。

## 使用限制与版权

本项目供受控的个人/小型聚会自托管测试使用。当前仅建议在 **localhost、可信局域网或 Tailscale** 访问；**不要直接向公网开放**。公网使用需要先补齐 HTTPS、房间访问控制、限速、媒体授权审查等措施。

源视频与歌词分别由其权利人拥有；能从公开接口获取，不等于允许再分发。请只处理具有相应使用权的媒体。歌词候选可能与翻唱版本不一致，用户需自行选取。自动对齐和渲染内核复制并适配自 [Volta233/AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK)，固定上游 commit、Apache-2.0 原始许可证及修改说明见 [third_party/autoassrenderer](third_party/autoassrenderer/README.md)；模型遵循各自条款。**本私有仓库暂未授予对外开源许可**；没有 LICENSE 文件，不代表代码可以自由再分发。
