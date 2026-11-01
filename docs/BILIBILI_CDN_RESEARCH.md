# B 站源直连 + HTML 歌词叠加的可行性实验（#111）

**结论（2026-11-01）**：已有字幕时间轴在 HTML 层叠加的技术路径可行，但**未经验证的 B 站 CDN URL 目前不适合作为 VC KTV 默认播放器**。保留现有已烧录 MP4 下载和电视播放；先使用 #107 的显式低码率成片适配 4 Mbps VPS 出口。独立实验页位于 `experiments/bilibili-overlay/`，不进入生产静态网页或 API。

## 三条媒体来源路径的实测

Spark-WSL 使用公开、免登录的 B 站 view/playurl 接口，对以下三首原始 BV 获取视频信息，无 Cookie、收费内容或绕过行为：

| BV | 360p HTML5 MP4 | 请求 720p DASH 的公开响应 | HP Chromium 直接 `<video src=...>` |
| --- | --- | --- | --- |
| `BV1jv411w7q6` | API 提供 URL | 最高公开 AVC 轨 480p（852×480），音视频分轨 | 360p / DASH 视频轨都失败 |
| `BV1gs411o7XG` | API 提供 URL | 最高公开 AVC 轨 480p（852×480），音视频分轨 | 360p / DASH 视频轨都失败 |
| `BV1Ls411m76o` | API 提供 URL | 最高公开 AVC 轨 480p（852×480），音视频分轨 | 360p / DASH 视频轨都失败 |

在 Spark 的无 B 站 Referer HTTP Range 探测中，上述 360p 直连和官方返回的备用 CDN 地址全部返回 403（另一次首选节点遇到 ConnectError）。HP 无头 Chromium 在自己的网络环境中加载六种地址时均显示 `MEDIA_ERR_SRC_NOT_SUPPORTED (4)`、视频尺寸 `0×0`、已缓冲 `0` 秒、播放进度 `0` 秒；对其中一首检查 Browser Network，确认浏览器直接向 `cn-bj-se-01-05.bilivideo.com` 发起媒体请求，随后失败为 `net::ERR_BLOCKED_BY_ORB`。请求没有经过 VC-KTV/VPS，**但没有成功播放**。在不同时间/网络环境曾有单首 360p 浏览器短播放成功的记录（原 #111），说明不能将成功或失败当成 B 站永久统一规则。

无凭证公开接口提供的是最高 **480p AVC**，不能因为请求参数 `qn=64` 就宣称取得了真正 720p；DASH 本身还需匹配独立音轨、混流、同步和缓冲。没有验证任何 720p **有声音**的跨站稳定播放方案，也没有运行依赖绕过防盗链或权限的实验。

B 站提供 [站外嵌入播放器](https://player.bilibili.com/player.html?bvid=BV1jv411w7q6)，使用 `https://player.bilibili.com/player.html?bvid=...`。HP Chromium 加载该 iframe 返回 200 并看到官方播放器内部的媒体资源，但父页面读取 iframe 内部 `document` / `<video>.currentTime` 实测返回 `SecurityError`（同源策略）。官方页面文档只列出 BV、初始 `t` 时间、autoplay 等参数，并无可依赖的持续进度/逐帧同步 API；不能把一次性的跳转能力冒充同步的 KTV 字幕时间轴。未经验证的第三方 postMessage 协议也不应成为生产依赖。

## HTML 时间轴叠加独立验证

`experiments/bilibili-overlay/index.html` + `overlay.js` 是无服务器依赖的隔离静态原型。它接受任意本地 MP4/公开媒体 URL 和上游 `ktv_app.subtitles.write_bundle` 导出的 `timeline.json`、`display_plan.json`；原生 `<video>` 提供播放、seek、时间和缓冲信息，`requestAnimationFrame` 根据 `video.currentTime` 选择双行歌词窗口，只对具有原始 Unit 时间的内容做比例高亮，没有模型时间的行保持静态。用户可调 -60 到 +60 秒的显示偏移。`window.overlaySnapshot()` 提供测试所需的时间、分辨率、卡顿和缓冲信息，不含权限凭据。

HP Chromium 实测：使用隔离生成的 **640×360、22 秒 MP4** 和从真实字幕尝试复制到独立实验目录的时间轴/展示计划，通过本地 file/blob 视频正常播放、暂停、seek 到约 8.49 秒，显示双行歌词；将偏移设为 +3 秒后，字幕采样时间相应变成约 5.51 秒。视频 `video.buffered` 为 `[0,22]`，尺寸 640×360，控制台没有 JavaScript 异常。此测试只验证 **HTML 展示与时间轴驱动**，因为隔离合成视频不是对应的歌曲录音，不能据此声称人耳同步准确。

浏览器 `preload="auto"` 与 `video.buffered` 不保证全曲缓存；可离线/局域网做积极缓存，但 CDN 防盗链、时间性 URL 及 4 Mbps 出口长期超码率是不同问题。特别是官方 360p 可能跨日或跨用户失效；新增客户端 HTML 模式不能取代高质量可下载、已烧录字幕的 MP4。

## 决策与以后重启本方向的条件

1. **暂不集成 B 站直连播放器到 VC KTV**。不改变生产配置、CDN 头、不做代理绕过、不把未授权媒体传到仓库。保留现有本地原片 + 字幕烧录 + 下载和 #104 的下一首预加载。
2. 即使以后直连 360p 在特定网络可用，也必须做到浏览器直接播放真实音视频、暂停/seek/断线/过期回退，并在有对应 B 站许可的条件下验证。若 720p DASH 可用，还需稳定的音视频组合和 MSE/CORS 证据。
3. 性能验收必须记录可用分辨率、VPS 出口流量、首帧、播放中断次数/时长、`video.buffered`、实际浏览器 Network 源、长间奏/逐字叠词同步和默认烧录回退，不只验证 API 返回 URL。多用户共享 4 Mbps 时还需考虑并发预算。

**原始证据**（只存在 Spark-WSL）：`~/.local/share/vc-ktv/pipeline/benchmarks/20261101-cdn-overlay/`，含 `query_urls.py`、`public-api-results.json`（不含签名 URL）、`playback-urls-ephemeral.json`（过期可能失效，切勿发布）、`browser-cdn-results.json`、`browser-overlay-results.json`、合成 MP4 和独立读取的历史时间轴。公开原型不嵌入任何 URL 或版权视频。终端测试使用 HP Chromium；临时服务器在验收后关闭。
