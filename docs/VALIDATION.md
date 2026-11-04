# 验证范围

自动测试通过独立 SQLite、模拟账号和媒体文件验证场次权限、点歌、字幕回退、排序、已唱歌曲下载、处理耗时与播放器状态；FFmpeg 集成测试检查带字幕视频及原音轨保持。CI 不下载真实 B 站视频和模型权重，也不进行 CUDA 推理。

实际 GPU 环境已验证过 Kim、TIFA、ASS/LRC 和 MP4 的独立运行，但字幕准确性仍取决于翻唱版本、重复歌词和长间奏。不能因单元测试或无报错就认为歌曲时间轴完全正确。

尚未验收真实包厢乐播 SDK 自动投屏，当前主要支持网页播放、屏幕镜像及管理员手动下载投屏。产品只面向可信网络，安全边界见 [SECURITY.md](SECURITY.md)。

## 分层验收

| 层级 | 验证范围 | 执行方式 | 边界 |
| --- | --- | --- | --- |
| 代码与离线集成 | FastAPI、隔离 SQLite、调度、权限、字幕回退、FFmpeg、前端语法 | `uv run --no-sync pytest -q`、`uv run --no-sync pre-commit run --all-files` | 不包含真实浏览器解码、GPU 或生产升级 |
| 真实浏览器 | 移动端加入/注册/登录/BV 点歌、原片选择、管理员上传、TV 播放、HTTP Range、下载 | 下面的 `scripts/browser_smoke.py` | 纯本地合成视频和模拟 B 站元数据；不验证模型准确率 |
| GPU 安装预检 | FFmpeg/libass、字体、模型和三套运行时文件 | `uv run --no-sync python -m scripts.check_install --mode gpu` | 不验证 CUDA 导入和真实推理 |
| 真实 GPU 与发布 | Kim/Qwen/TIFA、烧录、实际浏览器/网络、备份恢复、生产切换与回滚 | 单独选择测试机器和样例，人工记录结果 | 不纳入常规 CI；不得影响正在播放或制作的歌曲 |

### 可重复的真实浏览器 Smoke Test

在已有可运行基础服务依赖、FFmpeg/libx264 和 Chromium 系统库的 Linux 环境中，单独安装 Playwright Chromium，然后执行：

```sh
uv run --no-sync --with playwright python -m playwright install chromium
uv run --no-sync --with playwright python scripts/browser_smoke.py
```

脚本自动启动**仅监听随机 localhost 端口**的临时 FastAPI 服务，创建随机管理员/用户凭据、独立场次和 SQLite，关闭 GPU 自动准备，并生成一段 8 秒的 H.264/AAC 合成视频。它用真实 Chromium 验证移动端加入与注册、全新浏览器登录、点歌与选择原片、管理员 MP4 上传、电视端签名媒体的 Range 读取、实际播放结束及已唱歌曲下载。B 站视频元数据使用固定测试值，歌词候选请求在浏览器侧模拟为空。所有配置、数据库、媒体和测试进程在结束后清理，**不读取生产密钥、模型或数据，也不访问真实 B 站 API**。

Playwright 的浏览器二进制仍依赖操作系统共享库；如果启动报缺少 `libnspr4.so` / `libnss3.so` 等，需先在隔离测试环境准备依赖，不应为运行测试直接变更生产机器的系统库。浏览器本身不能启动时，不得报告为应用测试失败或通过。脚本使用可选的 Playwright 依赖，不将浏览器下载和操作系统安装引入常规 CI。

如果 Playwright 和项目运行环境使用不同的 Python，可用 `VC_KTV_APP_PYTHON=/path/to/project/.venv/bin/python` 指定临时服务的解释器。脚本在服务端模拟离线视频元数据，仍真实执行 HTTP、SQLite、媒体流和浏览器 JavaScript。

生产升级另需记录待办歌曲和 worker 状态、当前提交与配置、SQLite 一致性备份、媒体/场次目录副本、备份校验、回退提交及恢复演练。**在隔离副本上验证兼容性不等于允许对运行中的生产目录直接复制、回滚或重启。**
