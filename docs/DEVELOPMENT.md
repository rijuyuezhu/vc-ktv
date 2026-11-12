# 开发指南

项目使用 Python 3.12、FastAPI、SQLite 和原生 JavaScript。前端检查需要 Node.js。代码入口及数据流见 [系统架构](ARCHITECTURE.md)。

## 开发环境

```sh
uv sync --locked --group dev
npm ci
npm run format:check
uv run --no-sync pre-commit run --all-files
uv run --no-sync pyright
uv run --no-sync pytest -q
bash -n scripts/*.sh
bash scripts/check_js.sh
```

前端位于 `static/`，服务端位于 `vc_ktv/`，字幕引擎通过 `vc_ktv/pipeline/` 接入；`ktv_app/` 为固定版本的第三方源码。自动测试使用隔离数据库和合成媒体；多用户概率仿真与队列压力测试见 [benchmarks/](../benchmarks/README.md)。真实 GPU 环境可另外运行：

```sh
uv run --no-sync python -m scripts.gpu_smoke
```

需要浏览器端到端验证时：

```sh
uv run --no-sync --with playwright python -m playwright install chromium
uv run --no-sync --with playwright python scripts/browser_smoke.py
```

## 提交和部署

在功能分支开发，提交 PR、运行 CI、review 后合并。生产部署与源码合并分开执行，先确认场次没有正在播放或制作的任务。发布操作见 [部署与升级](RELEASE.md)。

模型及字幕引擎基于第三方代码，修改这些模块时保留来源与许可信息，详见 [第三方说明](../third_party/autoassrenderer/README.md)。
