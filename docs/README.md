# 文档导航

首次使用请阅读 [项目 README](../README.md)，然后根据用途选择文档。**安装与功能说明适用于当前代码；历史实验仅代表当时的样本和条件。**

## 安装与使用

- [安装与运行](INSTALL.md)：启动基础服务，按需安装 CUDA 模型，配置常驻服务
- [场次及数据](EVENTS.md)：场次、账号、历史记录和归档
- [点歌、自动制作与播放](AUTOPREP.md)：准备阶段、失败回退、公平轮播及管理员操作
- [歌词来源与 LRC](PROVIDER_LRC.md)、[字幕对齐](ALIGNMENT.md)、[HTML 字幕](TV_HTML_OVERLAY.md)

## 部署与维护

- [安全与反向代理](SECURITY.md)：身份验证、信任代理、适用网络
- [升级与回滚](RELEASE.md)：数据备份、隔离验收、生产切换
- [GPU 模型环境](SPARK_WSL_PIPELINE.md)：Linux CUDA 实测环境及推理验收
- [上游字幕引擎](UPSTREAM_PIPELINE.md)：实现差异与模型来源

## 开发与测试

- [开发指南](DEVELOPMENT.md)：代码结构、测试与协作流程
- [验证范围](VALIDATION.md)：离线测试、浏览器、GPU 与生产验收的边界

## 历史实验

以下报告保留当时的硬件、网络、样本和测量条件，不作为当前默认配置或性能保证。

- [4 Mbps 码率与浏览器缓冲](PERFORMANCE_4MBPS.md)
- [质量流水线三首歌曲并发](PERFORMANCE_QUALITY_THREE_WAY.md)
- [质量流水线两首歌曲并发](PERFORMANCE_QUALITY_THROUGHPUT.md)
- [B 站 CDN 直连与歌词叠加](BILIBILI_CDN_RESEARCH.md)

报告引用的部分原始数据保存在当时的隔离测试环境中，不随 Git 仓库分发。硬件规格、模型版本与通用实验路径是复现信息；实际账号、场次数据库、访问凭据、签名 URL 和用户媒体不属于公开实验材料。
