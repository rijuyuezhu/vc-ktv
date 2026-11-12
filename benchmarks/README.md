# 多用户基准测试

这些脚本用于验证点歌、歌词时间轴、取消、调度、公平性与主屏相关的状态竞争。**所有操作都在新建的临时 FastAPI + SQLite 场次中进行**：只绑定本机回环地址，结束后清理场次，不接入已有服务、生产场次或账号。

从仓库根目录运行，先安装开发依赖：`uv sync --locked`。

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

# 重唱改词的 #450 回归场景；严格检查可播放状态时加 --require-fixed
uv run --no-sync python -m benchmarks.replay_lyrics
```

Chromium 首次使用时需安装相应浏览器运行文件。移动端测试的歌词候选是本地夹具，页面、签名、提交与状态持久化是实际代码。

所有脚本均有 `--help`。可通过 `--output PATH` 指定 JSON 结果目录；默认写入被 Git 忽略的 `benchmarks/.results/`，**不要提交包含运行时诊断的结果文件**。

## 范围

- 使用虚构用户名、临时密码、本机测试地址和模拟歌曲 ID；公开的第三方歌词来源网址仅用于候选歌词的测试元数据。
- 概率场景包括虚拟重唱后的选词、偏移和跳过歌词；`replay_lyrics` 专门检查 #450 的源媒体继承、原片回退和重制可行性，`--require-fixed` 会把回归视为失败。
- 公平调度的补偿规则是累计歌数差距最多 2，**不保证每个人在前两轮一定轮到**。大队列脚本会检查前一轮参与者、队列响应时间和后续调度一致性。
- 这些基准不替代真实媒体、GPU 或不同电视硬件的端到端测试；不作为公网延迟或生产服务性能的数据来源。
