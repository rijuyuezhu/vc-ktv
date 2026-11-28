# 多用户基准测试

从仓库根目录运行 `uv sync --locked`。所有测试使用本机临时场次和账号，不接入生产服务，结束后清理数据。结果默认保存至 `benchmarks/.results/`（不提交 Git），可用 `--output PATH` 修改。

## Fast：Web 与制作调度

```sh
uv run --no-sync python -m benchmarks.preparation \
  --seed 31415 --users 40 --steps 1600 --max-orders 400 \
  --web-readers 12 --web-every 1
```

使用实际 API、SQLite 和任务领取逻辑，模拟下载及制作，验证公平调度、重制、伴奏缓存与并发操作。报告 Web 查询、操作延迟和动作覆盖率。快速冒烟可用 `--users 4 --steps 35 --max-orders 15 --web-readers 2`。

## Real：实际制作与主屏

安装 GPU 模型及运行时（见 [安装指南](../docs/INSTALL.md)），确认机器空闲。歌单格式见 [real_playlist.example.json](real_playlist.example.json)，支持 B站和网易云，歌词方式为 `provider`、`align` 或 `skip`；可指定 `query`，对齐任务也可提供 `lyrics_file`。使用已确认可播放的歌曲替换示例。

仅检查参数时不加 `--execute`。完整制作、重制及 API 播放验证：

```sh
uv run --no-sync python -m benchmarks.real_party \
  --manifest benchmarks/real_playlist.example.json --execute \
  --users 3 --refresh --playback api
```

多用户真实负载测试（需预先安装 Playwright Chromium）：

```sh
uv run --no-sync --with playwright python -m benchmarks.real_party \
  --manifest benchmarks/real_playlist.example.json --execute \
  --users 10 --additional-readers 30 --orders 15 --provider-ratio 0.8 \
  --submit-workers 3 --quality 1080 --codec av1 --bandwidth 3 \
  --pressure-seconds 150 --pressure-playback --continuous-seconds 1800
```

| 参数 | 用途 |
| --- | --- |
| `--orders N --provider-ratio 0.8 --skip-ratio 0 --seed 42` | 抽取歌曲，默认来源时间轴与对齐 4:1，优先不同来源 |
| `--repeat N` / `--timeout S` | 重复清单 / 完整制作等待上限 |
| `--general-workers` / `--fast-workers` | 普通、Fast Worker 数量（基准默认 4、2；伴奏 1） |
| `--burn-subtitles` | 提前烧录字幕，默认仅实时字幕 |
| `--pressure-seconds S` / `--additional-readers N` | 提交后观察 S 秒，中途增加查询者；不等待全部制作完成 |
| `--pressure-playback --continuous-seconds S` | 随后在 Chromium 中观察实际自动切歌、原唱与伴奏切换 |
| `--playback api/browser/manual` | 完整制作后验证签名媒体与重唱缓存、浏览器播放或人工试听 |
| `--refresh` | 完整制作后改变编码及字幕样式并更新 |

Real 记录歌曲就绪耗时、制作和伴奏状态、烧录产物、Web 延迟及整机 GPU 指标。连续播放还记录自然完播、服务端记录差异、媒体错误、停滞和伴奏切换时钟差。就绪时间受轮询影响，观察窗口包含等歌时间；媒体时钟差不是物理音画同步测量。Fast 不测真实编码或 GPU 性能，两种模式均不能代表公网延迟或实体电视体验。

## 概率用户

```sh
uv run --no-sync python -m benchmarks.party \
  --seed 31415 --users 40 --steps 1200 --max-orders 400 \
  --p-order 0.4 --p-cancel 0.2 --p-burst 0.3 \
  --p-netease 0.5 --p-bad-timing 0.5
```

模拟连点、选词、跳过、取消和中途加入，使用独立公平调度参考算法核对结果。`p-order`、`p-cancel` 等为动作相对权重，`p-burst` 等为条件概率；`--p-provider-timing` 控制来源时间轴比例（默认 0.8）。相同种子便于复现操作序列，并发执行顺序仍可能不同。各脚本的完整参数见 `--help`。

## 独立场景

```sh
# 多人各点若干首，全部变成已准备状态，再执行随机操作
uv run --no-sync python -m benchmarks.mass_queue \
  --users 60 --initial-users 48 --songs-per-user 12 --steps 950

# 取消 vs 选词 / 置顶 / 播放；跳过字幕 vs 选词
uv run --no-sync python -m benchmarks.races \
  --seed 2026 --users 12 --rounds 35

# 真实 Chromium 手机视口：选词、正负偏移、越界恢复和逐行删词
uv run --no-sync --with playwright==1.64.1 python -m benchmarks.lyric_ui \
  --offsets 0.3,-0.3,-100,1.5 --invalid-offset=-100

# 重唱改词与媒体复用
uv run --no-sync python -m benchmarks.replay_lyrics
```

Chromium 首次使用时需安装相应浏览器运行文件。移动端测试的歌词候选是本地夹具，页面、签名、提交与状态持久化是实际代码。

## 范围

- 概率场景包括虚拟重唱后的选词、偏移和跳过歌词；`replay_lyrics` 检查源媒体继承、原片回退和重制可行性，`--require-fixed` 会把回归视为失败。
- 公平调度的补偿规则是累计歌数差距最多 2，**不保证每个人在前两轮一定轮到**。大队列脚本会检查前一轮参与者、队列响应时间和后续调度一致性。

## 伴奏分离

主唱分离方法比较见 [BACKING_VOCALS.md](BACKING_VOCALS.md)。仅提交实验脚本与测试，模型权重和音频样本不入库。
