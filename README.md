# VC KTV

基于 B 站 BV 号的自托管 KTV 点歌系统。使用 Python、FastAPI、SQLite 和静态网页，不需要 Docker、Node.js 或云端 LLM。适用于可信局域网和 Tailscale，**不建议直接向公网开放**。

## 使用

1. 管理员创建场次，将六位加入码发给大家。
2. 用户进入首页，输入加入码后注册或登录本场账号，再粘贴 BV 号或 B 站视频链接点歌。
3. 在线歌词由独立来源陆续提供候选（VCPedia、网易云音乐、LRCLIB），用户自行选择，也可上传 UTF-8 LRC（重新对齐时间戳），后台下载原视频并制作带字幕的视频。歌曲可以点开查看详情、下载或取消自己的待播歌曲（每人同时最多保留 10 首）；本场已唱歌曲也可查看和下载。
4. 管理员在管理首页查看队列、控制播放、下载当前视频；从「设置」管理场次与字幕选项，从「演唱统计」查看本场记录。管理员可重试字幕、把已可播放的待播歌曲指定为下一首，或安排已唱歌曲下一首重唱。新的下一首安排会替换旧的优先标记，不会打断当前播放。

公平轮播是默认队列策略：只看已唱首数，不比较歌曲时长；同一人最多获得两首的落后补偿，人数增多仍按轮次轮流。只有领取普通待播歌曲时才持久化调度补偿；未播放就取消不计数，普通歌曲领取后跳过仅消耗虚拟轮次。管理员置顶及重唱视为额外管理员轮次，不增加任何用户的演唱统计或公平计数；也可以切换为先来先唱。

默认下载不高于 720p 的原片，可在设置中改为 480p。字幕默认采用 **Kim 人声分离 + 双遍 Qwen3-ASR 定位 + 分块 TIFA 对齐与接缝修复**。拟声词仅在对齐阶段替换读音，字幕仍显示原始歌词。成片保留源视频音轨。管理员可设置场次级在线播放带宽预算（默认 3 Mbps），新烧录成片与电视原片缓存共用该限制。自动对齐属于 best effort：不因切块时长超标而拒绝制作，模型对齐失败会烧录有明确标记的估计字幕；歌词和时间仍可能有误。

## 安装

需要 Linux、Python 3.12、uv、FFmpeg（libass/libx264）和 Noto CJK 字体。基础服务与完整 CUDA 模型环境独立安装；首次在新机器部署请先阅读 [安装与运行](docs/INSTALL.md)，不要直接套用原作者的服务配置。

```sh
# 以下仓库当前需要 GitHub 访问权限；git、gh、uv 和 FFmpeg 安装见指南
gh repo clone rijuyuezhu/vc-ktv
cd vc-ktv
uv sync --locked
./scripts/setup.sh
uv run --no-sync python -m scripts.check_install --mode base
VC_KTV_AUTOPREPARE=0 uv run --no-sync python -m vc_ktv
```

访问 `http://127.0.0.1:8778/` 和 `http://127.0.0.1:8778/admin/`。该命令仅运行基础服务，不会自动下载或制作新歌曲；完整 GPU 字幕部署及模型依赖请按照 [安装指南](docs/INSTALL.md) 逐步设置和验收。开发测试：`uv run pytest -q`、`uv run pre-commit run --all-files`。

## 文档

- [完整文档导航](docs/README.md)：使用、部署、开发、验证与历史实验
- [安装与运行](docs/INSTALL.md)：基础服务与可选 GPU 字幕
- [场次与点歌](docs/EVENTS.md)、[字幕制作与播放](docs/AUTOPREP.md)
- [安全与代理部署](docs/SECURITY.md)、[升级与回滚](docs/RELEASE.md)
- [开发指南](docs/DEVELOPMENT.md)、[测试与验证](docs/VALIDATION.md)

本项目是私人测试原型，不提供公网投屏服务或原生乐播 SDK。视频和歌词版权属于各权利人，请仅处理有权使用的素材。音频分离、对齐及字幕渲染代码基于 [AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK) 修改，固定来源、Apache-2.0 许可及归属见 [第三方说明](third_party/autoassrenderer/README.md)；模型也适用各自条款。本仓库代码采用根目录 [LICENSE](LICENSE) 中的 Apache-2.0 许可；第三方内容仍遵循各自许可。
