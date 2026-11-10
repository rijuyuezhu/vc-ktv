# 安装与运行

以 Debian/Ubuntu + Python 3.12 为例，需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

## 安装基础服务

```sh
sudo apt-get update
sudo apt-get install -y git curl ffmpeg fontconfig fonts-noto-cjk fonts-noto-color-emoji
# 安装 uv（已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh
# 将 uv 加入 PATH 后：
git clone https://github.com/rijuyuezhu/vc-ktv.git
cd vc-ktv
uv sync --locked
./scripts/setup.sh
uv run --no-sync python -m scripts.check_install --mode base
```

初始管理员密码：

```sh
cat ~/.config/vc-ktv/admin-password
```

## 启动

只使用场次、管理和已有视频：

```sh
VC_KTV_AUTOPREPARE=0 uv run --no-sync python -m vc_ktv
```

启用自动下载和来源时间轴字幕，不运行 GPU 对齐：

```sh
VC_KTV_GPU_ALIGNMENT=0 uv run --no-sync python -m vc_ktv
```

默认访问地址为 `http://127.0.0.1:8778/`，管理员页面为 `/admin/`。可用 `curl -fsS http://127.0.0.1:8778/api/health` 检查服务。

## GPU 字幕（可选）

使用 NVIDIA CUDA 及 Kim、Qwen3-ASR、TIFA。模型与 CUDA 运行时单独存放，默认位于 `~/.local/share/vc-ktv/pipeline/`。

```sh
sudo apt-get install -y espeak-ng
uv run --no-sync python -m scripts.setup_pipeline_assets all

export VC_KTV_PIPELINE_ROOT="$HOME/.local/share/vc-ktv/pipeline"
CUDA_VENV="$VC_KTV_PIPELINE_ROOT/runtime/cuda-torch"
uv venv --python 3.12 --no-project "$CUDA_VENV"
uv pip install --python "$CUDA_VENV/bin/python" --torch-backend cu130 \
  'torch==2.13.0+cu130' 'torchaudio==2.11.0+cu130'
uv run --no-sync python -m scripts.setup_tifa_runtime \
  --root "$VC_KTV_PIPELINE_ROOT" --torch-python "$CUDA_VENV/bin/python"
uv run --no-sync python -m scripts.setup_pipeline_runtimes all --root "$VC_KTV_PIPELINE_ROOT"
uv run --no-sync python -m scripts.check_install --mode gpu
uv run --no-sync python -m scripts.gpu_smoke
uv run --no-sync python -m vc_ktv
```

该 CUDA 安装组合已在 Linux AArch64 / CUDA 13.0 环境验证。其他架构和 CUDA 版本请先确认对应的 PyTorch 包可用。

## 常用配置

| 环境变量 | 默认 | 用途 |
| --- | --- | --- |
| `VC_KTV_HOST` / `VC_KTV_PORT` | `127.0.0.1` / `8778` | 服务监听地址 |
| `VC_KTV_CONFIG` | `~/.config/vc-ktv` | 管理员密码和私密配置 |
| `VC_KTV_DATA` | `~/.local/share/vc-ktv` | 场次数据与媒体 |
| `VC_KTV_AUTOPREPARE` | `1` | 自动下载与制作 |
| `VC_KTV_GPU_ALIGNMENT` | `1` | GPU 字幕对齐 |
| `VC_KTV_PREP_WORKERS` | `1` | 普通 Worker 数量（1–4，可处理 GPU 对齐） |
| `VC_KTV_FAST_WORKERS` | `0` | 额外 CPU Fast Worker 数量（0–2，只处理来源时间轴任务；生产推荐 2） |
| `VC_KTV_PIPELINE_ROOT` | `~/.local/share/vc-ktv/pipeline` | 模型路径 |
| `KTV_FFMPEG` / `KTV_FFPROBE` | `ffmpeg` / `ffprobe` | 自定义媒体工具路径 |

GPU 对齐的高级参数：`VC_KTV_PIPELINE_MODE` 默认为 `quality`，还可使用 `fast`、`global`、`global-kim`；`VC_KTV_KIM_SINGLE_FLIGHT=1` 可让 Kim 音频分离任务串行执行。

常驻部署和升级操作见 [部署与升级](RELEASE.md)。
