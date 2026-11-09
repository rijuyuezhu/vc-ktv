# VC-KTV

VC-KTV 是一个自托管 KTV 点歌系统，支持 **B站视频**和**网易云音乐**。大家用手机点歌，服务自动准备歌词与视频，再通过网页播放器在电视上播放。

## 快速开始

需要 Linux、Python 3.12、[uv](https://docs.astral.sh/uv/) 和 FFmpeg。公开源码可直接克隆。

```sh
git clone https://github.com/rijuyuezhu/vc-ktv.git
cd vc-ktv
uv sync --locked
./scripts/setup.sh
VC_KTV_AUTOPREPARE=0 uv run --no-sync python -m vc_ktv
```

打开 [点歌页面](http://127.0.0.1:8778/) 或 [管理员页面](http://127.0.0.1:8778/admin/)。初始管理员密码可通过以下命令查看：

```sh
cat ~/.config/vc-ktv/admin-password
```

上面的启动方式先关闭自动制作，适合体验场次和点歌流程。开启自动下载、来源歌词或 GPU 对齐，参见 [安装与运行](docs/INSTALL.md)。

## 怎么用

1. 管理员创建场次，分享加入码；如需网易云点歌，先在「设置 → 网易云音乐连接」登录。
2. 大家加入场次，选择 B站或网易云，搜索歌曲或粘贴链接点歌，然后选择歌词与制作方式。
3. 管理员管理队列、控制播放；电视打开独立播放页即可唱歌。

详细操作见 [使用指南](docs/USAGE.md)。

## 文档

- [使用指南](docs/USAGE.md)：点歌、歌词、管理员及电视播放
- [安装与运行](docs/INSTALL.md)：基础服务、CPU 模式与 GPU 模式
- [系统架构](docs/ARCHITECTURE.md)：主要组件、任务流程与数据目录
- [开发指南](docs/DEVELOPMENT.md)：源码结构、测试与开发约定
- [部署与升级](docs/RELEASE.md) · [安全与网络](docs/SECURITY.md)

字幕引擎基于 [AutoASSrenderer4karaOK](https://github.com/Volta233/AutoASSrenderer4karaOK)，来源与许可见 [第三方说明](third_party/autoassrenderer/README.md)。音视频、歌词及模型遵守各自的许可。

# LICENSE

Apache License 2.0. See [LICENSE](LICENSE).
