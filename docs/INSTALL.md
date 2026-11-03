# 安装与运行

VC KTV 是 Python 3.12 + FastAPI + SQLite + 静态网页的自托管服务。**默认仅监听本机**；用于可信局域网或 Tailnet，不适合直接裸露到公网。以下命令面向有仓库访问权限的 Linux 用户，**不依赖任何原作者的主机、用户名或数据**。

## 1. 基础服务（不需要 GPU）

以 Debian/Ubuntu 为例安装系统依赖（其他发行版使用对应包管理器）：

```sh
sudo apt-get update
sudo apt-get install -y git gh curl ffmpeg fontconfig fonts-noto-cjk fonts-noto-color-emoji
```

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（例如 `curl -LsSf https://astral.sh/uv/install.sh | sh`），并确认 `uv --version`。仓库目前是私有的，需要 GitHub 访问权限。可先通过 `gh auth login` 登录，然后：

```sh
gh repo clone rijuyuezhu/vc-ktv
cd vc-ktv
uv sync --locked
./scripts/setup.sh
uv run --no-sync python -m scripts.check_install --mode base
```

也可以使用已经有权限的 Git HTTPS/SSH 地址（例如 `git clone https://github.com/rijuyuezhu/vc-ktv.git`）；私有仓库须先配置相应 Git 凭据。首次初始化会在 `~/.config/vc-ktv/` 下创建权限受限的管理员密码和签名密钥；**不要提交或公开这些文件**。管理员初始密码在本机使用 `cat ~/.config/vc-ktv/admin-password` 查看。

仅检查页面、管理员登录、场次和手动操作时，可以先关闭后台自动准备，避免模型尚未安装就触发任务：

```sh
VC_KTV_AUTOPREPARE=0 uv run --no-sync python -m vc_ktv
```

访问 `http://127.0.0.1:8778/`、`http://127.0.0.1:8778/admin/`，或者执行 `curl -f http://127.0.0.1:8778/api/health` 检查服务。关闭自动准备时，不会自动下载和制作新点播歌曲；要使用完整的无人值守字幕制作，需要完成下一节的 GPU 环境。

## 2. 完整 GPU 字幕（单独准备）

默认 `quality` 模式需要 NVIDIA CUDA、可用的 PyTorch + TIFA 运行时，以及 Kim、Qwen3-ASR、TIFA 权重。模型体积大，**不随源码分发**。下载模型文件与准备 CUDA Python 环境是两件事。

```sh
# 获取锁定的 TIFA / Kim / Qwen 源码、字典和权重（可能下载数 GB）
uv run --no-sync python -m scripts.setup_spark_assets all
```

`all` 包含 `tifa`、`kim`、`qwen` 三组，不包括上游 Windows FFmpeg 包；可分别指定组，或使用 `--root /custom/pipeline`。每个文件使用仓库锁定的 SHA-256 校验，已经存在的非受控源码目录不会被自动覆盖。对已经手工部署过 TIFA 的机器，不要直接运行 `all` 企图覆盖旧模型；先核对现有资源，必要时单独选择 `kim` / `qwen` 分组。

**仍需独立准备 `<pipeline>/runtime/tifa/bin/python`：** 它必须是一个实际可导入 CUDA PyTorch 和 TIFA 依赖的 Python 3.12 环境。当前仓库**尚没有**针对全新机器通用、经过验收的 TIFA CUDA 安装器；不同架构/CUDA 组合不能直接复用开发机器的安装步骤。该步骤未就绪前，不能宣称完整 GPU 部署成功。

完成 TIFA CUDA 环境后，才运行 Kim/Qwen 环境叠加安装：

```sh
uv run --no-sync python -m scripts.setup_spark_runtimes all
uv run --no-sync python -m scripts.check_install --mode gpu
```

自定义根目录时，设置 `VC_KTV_PIPELINE_ROOT` 可统一两个 setup 命令与应用的默认路径；也可向 setup 显式传入 `--root`、向检查命令传入 `--pipeline`。运行时脚本也支持显式 `--tifa-python /path/to/python`。

运行时安装脚本会检查共享 TIFA Python 和新装 overlay 是否实际看到 CUDA；但 **GPU 安装检查只验证必要文件/可执行文件的存在，不验证 CUDA 设备、模型推理或字幕准确性。** 启用自动准备前，还必须在隔离目录、使用有权处理的样例视频和歌词运行一次 [完整流水线测试](SPARK_WSL_PIPELINE.md)，再根据结果设置 `VC_KTV_AUTOPREPARE=1`。当前实际验证过的组合主要是 Linux ARM64 / WSL + NVIDIA CUDA；其他 CPU/GPU/OS 组合需要独立适配和测试。完整 GPU 自助安装和平台矩阵仍由 [#149](https://github.com/rijuyuezhu/vc-ktv/issues/149) 持续跟进。

## 3. 运行配置与可选常驻服务

主要配置由环境变量提供，私人配置和媒体数据分别默认保存在 `~/.config/vc-ktv`、`~/.local/share/vc-ktv`，不要放到源码仓库中：

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `VC_KTV_HOST` | `127.0.0.1` | 监听地址，不要随意绑定公网 |
| `VC_KTV_PORT` | `8778` | HTTP 端口 |
| `VC_KTV_CONFIG` | `~/.config/vc-ktv` | 私人密钥及密码 |
| `VC_KTV_DATA` | `~/.local/share/vc-ktv` | 场次、数据库和媒体 |
| `VC_KTV_AUTOPREPARE` | `1` | 是否启动自动下载/字幕准备 |
| `VC_KTV_PIPELINE_ROOT` | `~/.local/share/vc-ktv/pipeline` | GPU 模型与运行时根目录 |
| `VC_KTV_PIPELINE_MODE` | `quality` | 当前默认字幕流水线 |
| `VC_KTV_PREP_WORKERS` | `1` | 最多同时准备歌曲数（1–4） |
| `VC_KTV_KIM_SINGLE_FLIGHT` | 未设 | 设为 `1` 时序列化 Kim GPU 推理 |
| `VC_KTV_TRUSTED_PROXY_IPS` | 未设 | 精确可信代理 IP，详见 [安全说明](SECURITY.md) |

可选的 `systemd --user` unit 示例（将 **两处** `/absolute/path/to/vc-ktv` 换成实际 clone 目录）：

```ini
[Unit]
Description=VC KTV
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/vc-ktv
ExecStart=/absolute/path/to/vc-ktv/.venv/bin/python -m vc_ktv
Environment=VC_KTV_AUTOPREPARE=0
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

保存到 `~/.config/systemd/user/vc-ktv.service` 后，用 `systemctl --user daemon-reload` 和 `systemctl --user enable --now vc-ktv.service` 启用。示例默认只启动基础服务；完成 GPU 验收后再调整 `VC_KTV_AUTOPREPARE`。**不要在已有生产实例上直接套用模板或覆盖其 unit**。更新前先检查当前队列/准备任务，备份数据；合并 PR 也不意味着要立即部署。

### 媒体下载的临时链接

发送视频时，服务会在媒体所在目录创建 `.vc-ktv-inflight-*` 临时硬链接，响应结束或中断时自动删除。Linux/macOS 上，若进程异常退出，下次启动会根据数据目录中的所有者锁清理**已退出实例**留下的链接，不会清理仍有实例持锁的链接，也不会删除原视频。

旧版本没有所有者锁的临时链接无法安全判定是否仍被使用，因此不会自动清理。需要手动处理时，务必**先停止所有使用同一数据目录的服务实例**，再检查并清理残留文件；不要在播放或下载过程中直接批量删除。

## 4. 验证范围

常规 CI 通过模拟媒体、隔离 SQLite、合成字幕和前端语法检查验证核心行为，不下载权重或访问 B 站。开发者可运行 `uv run pytest -q` 和 `uv run pre-commit run --all-files`。涉及真实 CDN、CUDA、浏览器或带宽的验证应另外记录软硬件和测试条件。

源码及模型/视频遵守各自许可；上游改造与来源见 [第三方说明](../third_party/autoassrenderer/README.md)。
