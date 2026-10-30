# 开发指南

- 应用使用 Python 3.12、FastAPI、SQLite 与原生 JS，不引入第二套前端框架或后台进程。
- 使用 `uv.lock` 管理依赖；测试必须使用隔离数据库、合成媒体，不允许操作正在使用的场次或私密配置。
- 先提交 PR，经测试和审查后合并。合并不等于部署；部署前先确认无正在播放或制作的任务，备份数据并进行服务健康检查。

```bash
uv sync --locked --group dev
uv run pre-commit run --all-files
uv run pytest -q
bash -n scripts/*.sh
```

CI 另检查静态 JS 语法。真实模型和设备测试单独执行，不要求普通单测下载模型权重或访问 B 站。

## 代码位置

| 路径 | 作用 |
| --- | --- |
| `vc_ktv/app.py` | HTTP、场次数据与队列、权限和媒体接口 |
| `vc_ktv/autoprep.py` | 下载及制作任务、进度与失败回退 |
| `vc_ktv/global_alignment.py` | 全曲 TIFA 对齐和发音处理 |
| `vc_ktv/upstream_pipeline.py` | 本地模型及字幕生成入口 |
| `ktv_app/` | 复用和修改的上游字幕引擎 |
| `static/` | 点歌、管理与电视播放网页 |
| `tests/` | 独立行为测试和上游算法测试 |

用户确认歌词，模型只负责对齐。原片和每次字幕尝试独立保存，**不可为可读性或性能清理而删除失败路径与可靠的回归测试**。更新上游代码需保留来源及许可信息，参见 [UPSTREAM_PIPELINE.md](UPSTREAM_PIPELINE.md)。实际生产模型及资产见 [SPARK_WSL_PIPELINE.md](SPARK_WSL_PIPELINE.md)。
