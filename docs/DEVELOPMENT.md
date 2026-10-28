# 开发指南

## 基本约束

- 目标 Python 3.12；使用 uv 和 uv.lock 管理应用依赖。运行时不用 Docker、npm、外部 Agent。
- 保持 FastAPI / SQLite / 静态浏览器前端的 KISS 架构；不为单个 edge case 引入复杂平台或额外守护进程。
- 不允许 CI、测试或 Git 操作修改真实生产数据。测试应使用 pytest tmp_path 和 TestClient。
- 敏感配置、下载媒体、权重和实验数据只存在于本机：.runtime/、~/.config/vc-ktv 和 ~/.local/share/vc-ktv，不提交 Git。
- 管理员保持一次性启动播放的使用习惯；歌词选择属于提交 BV 的普通用户。

## 启动与校验

~~~bash
uv sync --locked --group dev
./scripts/setup.sh
VC_KTV_HOST=127.0.0.1 uv run python -m vc_ktv
~~~

~~~bash
# 本地安装提交前检查
uv run pre-commit install

# 与 CI 一致的检查
uv run pre-commit run --all-files
uv run pytest -q
bash -n scripts/*.sh
~~~

pre-commit 执行大型文件限制、私钥检测、冲突标记和 TOML/YAML 基本检查，并通过固定在 uv.lock 中的 Ruff 做 F/I lint 与格式检查。CI 在 Ubuntu / Python 3.12 上重复这些检查，额外运行 JavaScript 语法检查。

## 代码导航

| 模块 | 职责 |
| --- | --- |
| vc_ktv/app.py | HTTP API、Cookie 授权、SQLite、排队、公平播放、媒体签名链接 |
| vc_ktv/autoprep.py | 非阻塞媒体准备、TIFA 可选增强及失败回退 |
| vc_ktv/bilibili_media.py | yt-dlp 页面解析失败时的公开 B 站播放流备用下载 |
| vc_ktv/lyric_selection.py | 多来源歌词搜索与候选整理 |
| vc_ktv/lyric_sources.py | VCPedia 歌词正文解析 |
| vc_ktv/textgrid_import.py | TIFA TextGrid → 增强 LRC |
| vc_ktv/karaoke.py | 解析 LRC、生成 ASS 和 FFmpeg 烧录 |
| static/app.js | 点歌者/管理员交互，歌词预览与选择 |
| static/tv.js | 有声播放初始化、结束回调、下一首自动播放 |

歌词候选由点歌者选择（不会自动猜测正确版本）；候选选择只允许点歌者的原浏览器 Cookie，并绑定 30 分钟签名摘要。视频就绪即进入队列，字幕增强失败不影响原片播放。如果歌曲已开始播放，后续对齐不会中途替换播放流。

## 开发与生产环境隔离

真实媒体位于 VC_KTV_DATA 指定目录；不要把单元测试指向正式数据库。不要在跑 GitHub CI 时添加需要 GPU 或公网媒体接口的测试，除非显式标记并有独立配置/测试凭据。实机记录及模型版本见 ALIGNMENT.md、VALIDATION.md。

建议先 PR，再合并 main；保持 CI 绿色。用户尚未授权正式公开发布，因此不应建立 public 仓库、Release 或对外 License。GitHub 仓库先保持 private。
