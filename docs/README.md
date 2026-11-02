# 文档导航

本项目使用 Python/FastAPI、SQLite 和原生网页构建个人自托管 KTV。开始使用请先阅读 [根 README](../README.md)，之后按需要进入下列内容。产品操作和部署说明以**当前代码行为**为准；历史实验报告不是安装步骤或生产状态公告。

## 使用、运维与开发

- [场次及数据](EVENTS.md)：创建、关闭、归档场次，账号与历史记录
- [点歌、自动制作与播放](AUTOPREP.md)：准备阶段、失败回退、公平轮播与管理员操作
- [安全边界](SECURITY.md)：只在可信网络部署、权限与反向代理要求
- [开发指南](DEVELOPMENT.md)：代码位置、测试和 PR/部署边界
- [功能验证范围](VALIDATION.md)：自动化与真实设备测试的区别
- [来源 LRC 模式](PROVIDER_LRC.md)、[字幕对齐](ALIGNMENT.md)、[HTML 字幕](TV_HTML_OVERLAY.md)
- [上游引擎](UPSTREAM_PIPELINE.md)、[模型环境记录](SPARK_WSL_PIPELINE.md)：模型、许可证与 CUDA 运行条件

## 历史实验与研究结果

以下报告记录**特定日期、硬件、网络和样本**的测量，目的是保留方法、指标和局限，并非现行部署建议或对所有环境的性能保证：

- [4 Mbps 码率与浏览器缓冲](PERFORMANCE_4MBPS.md)
- [质量流水线三首歌曲并发](PERFORMANCE_QUALITY_THREE_WAY.md)
- [质量流水线两首歌曲并发](PERFORMANCE_QUALITY_THROUGHPUT.md)
- [B 站 CDN 直连与歌词叠加实验](BILIBILI_CDN_RESEARCH.md)

实验中列出的 GPU 型号、CPU 架构、浏览器与时间戳属于复现条件，应予保留。某些报告还引用原作者**本机未入库的原始数据**，仅作为证据定位，不意味着 Git clone 后可直接访问。不要向公开 issue/PR 上传含有会话数据库、用户数据、Cookie、签名媒体 URL、未授权音视频的原始产物。

如需实际复测，请用自己合法获取的媒体和隔离目录运行，而不是访问原作者的机器或在线生产场次。涉及 GPU、外部歌词来源或真实电视投屏的结论，必须另行记录运行环境和验收结果。
