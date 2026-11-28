# 多用户基准测试

这些脚本用于验证点歌、歌词时间轴、取消、调度、公平性与主屏相关的状态竞争，以及真实媒体制作。**所有操作都在新建的临时 FastAPI + SQLite 场次中进行**：只绑定本机回环地址，结束后清理场次，不接入已有服务、生产场次或账号。

从仓库根目录运行，先安装开发依赖：`uv sync --locked`。

## Fast 与 Real

Fast 模式验证 Web、队列和制作状态机，不下载歌曲、不执行 FFmpeg 或 GPU 推理。Real 模式启动实际制作 Worker，下载真实歌曲、对齐字幕、分离伴奏，并验证主屏播放。两者都使用独立的本机场次，不接入生产服务。

### Fast：Web 和制作调度

```sh
uv run --no-sync python -m benchmarks.preparation \
  --seed 31415 --users 40 --steps 1600 --max-orders 400 \
  --web-readers 12 --web-every 1
```

复用下文的概率用户行为。普通、Fast、伴奏三类制作任务使用实际领取逻辑和合成媒体，分别核对预计演唱顺序、任务资格、累计演唱次数和缓存状态。包含个人队列置顶、伴奏就绪限制、编码/分辨率/码率/字幕样式变更、增量重制、重唱和取消。并发读取用户及管理员状态，与随机写操作交错执行；报告读取与操作耗时的 P50/P95/最大值，以及各类动作和调度核验次数。随机场景的覆盖程度以结果中的动作计数为准。

轻量冒烟测试：`--users 4 --steps 35 --max-orders 15 --web-readers 2`。Fast 模式使用合成媒体，不能检验真实编码、声画同步或 GPU 吞吐；并发 Web 压力也不是公网性能测试。

### Real：实际制作与主屏

歌单 JSON 格式见 [real_playlist.example.json](real_playlist.example.json)，支持 B站 BV/分P、网易云单曲 ID 或分享链接、`align`（实际 GPU 对齐）、`provider`（来源时间轴）、`skip`（原片）。可指定 `query` 精确搜索歌词；`align` 也支持相对于歌单的 `lyrics_file`。示例歌单尚未联网验证，实际运行前可替换成确认能播放的歌曲。

仅检查参数（不启动服务器、不访问来源、不使用 GPU）：

```sh
uv run --no-sync python -m benchmarks.real_party \
  --manifest benchmarks/real_playlist.example.json
```

确认机器空闲且已安装模型、CUDA 运行时后，才显式添加 `--execute`：

```sh
uv run --no-sync python -m benchmarks.real_party \
  --manifest benchmarks/real_playlist.example.json --execute \
  --users 3 --general-workers 4 --fast-workers 2 --quality 1080 \
  --refresh --playback api
```

Real 使用真实来源及 Worker，强制伴奏就绪，记录观察到的歌曲就绪耗时（从提交开始，受轮询和提交批次影响）。`--refresh` 在制作完成后切换编码和字幕样式并触发增量重制。`--playback api` 验证主屏配对、签名媒体 Range、字幕接口和重唱伴奏缓存；它不解码媒体。

另可将播放模式改为 `--playback browser`，并通过 `uv run --no-sync --with playwright` 启动（需预先安装 Chromium）。实际主屏播放第一首歌曲，切换原唱/伴奏并采样两个媒体时钟、暂停与解码错误状态。媒体时钟差不是物理音画同步测量。`--playback manual` 提供临时主屏配对地址，供本机浏览器或 SSH 隧道手动试听；按 Enter 结束。真实电视、音响和投屏延迟仍需人工体验。

Real 不自动安装模型，不共享生产账号和场次；临时数据在测试结束后清理。运行 GPU 及真实来源测试前应确认机器空闲。

## 概率用户

```sh
uv run --no-sync python -m benchmarks.party \
  --seed 31415 --users 40 --steps 1200 --max-orders 400 \
  --p-order 0.4 --p-cancel 0.2 --p-burst 0.3 \
  --p-netease 0.5 --p-bad-timing 0.5
```

不同用户具有连点、慎选歌词、跳过歌词、中途加入等行为偏好。可自定义所有动作的权重、歌词时间轴偏移概率、候选歌词逐行选择概率、场次人数、操作数和每隔多少步检查一次。`--help` 查看完整参数。

`p-order`、`p-cancel`、`p-join` 等是每步动作的**相对权重**；`p-burst`、`p-netease`、`p-bad-timing` 等是命中相应条件后的概率。相同种子便于重现操作序列；并发线程执行的先后顺序不承诺完全一致。

随机模拟器用实际 HTTP 登录、歌词候选签名、API 与 SQLite 状态转换，但媒体制作是**模拟 Worker 完成**，不执行网络下载或 GPU 对齐。内置独立调度参考算法，并核验歌曲状态、用户累计歌数、取消权限和来源时间轴。

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

所有脚本均有 `--help`。可通过 `--output PATH` 指定 JSON 结果目录；默认写入被 Git 忽略的 `benchmarks/.results/`，**不要提交包含运行时诊断的结果文件**。

## 范围

- 使用虚构用户名、临时密码、本机测试地址和模拟歌曲 ID；公开的第三方歌词来源网址仅用于候选歌词的测试元数据。
- 概率场景包括虚拟重唱后的选词、偏移和跳过歌词；`replay_lyrics` 检查源媒体继承、原片回退和重制可行性，`--require-fixed` 会把回归视为失败。
- 公平调度的补偿规则是累计歌数差距最多 2，**不保证每个人在前两轮一定轮到**。大队列脚本会检查前一轮参与者、队列响应时间和后续调度一致性。
- 这些基准不替代真实媒体、GPU 或不同电视硬件的端到端测试；不作为公网延迟或生产服务性能的数据来源。

## 伴奏分离

主唱分离方法比较见 [BACKING_VOCALS.md](BACKING_VOCALS.md)。仅提交实验脚本与测试，模型权重和音频样本不入库。
