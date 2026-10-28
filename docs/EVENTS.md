# KTV 场次与本地数据

一场 KTV 对应独立的 SQLite 和媒体目录，默认最多一场活动 KTV：

    $VC_KTV_DATA/
      active-session                 # 活动场次 ID
      sessions/
        20261028-.../
          info.json                  # 场次信息及六位加入码（仅管理员可读）
          room.sqlite3               # 本场人员、歌曲、统计和播放状态
          media/                     # 本场源视频、成品及对齐归档

管理员通过 /admin/ 创建或结束场次。结束后数据仍在目录中，可自行打包或清理；没有跨场次用户身份、队列或媒体共享。新场次采用六位加入码和本场临时账号：参与者输入加入码后注册用户名与个人密码（重复确认），注册后直接登录；账号及带盐密码哈希只在该场 SQLite 内。结束后下一场需重新加入、注册。旧场次数据仍可由管理员归档。

旧版本的 room.sqlite3 与 media/ 保持原位，应用识别为 legacy 场次，**不会在服务启动时擅自搬移或丢弃数据**。当结束这场旧 KTV 后，可以将其作为一个目录归档：

    python scripts/archive_legacy.py --root ~/.local/share/vc-ktv
    python scripts/archive_legacy.py --root ~/.local/share/vc-ktv --apply

归档脚本先生成 SQLite 一致性副本并核对歌曲/用户数、媒体文件列表及大小，保存到 sessions/legacy/，再清理旧布局。确认没有其他进程仍访问旧数据库或媒体时才运行该脚本；生产部署前应另做备份并在隔离测试目录演练。此步骤不是正常启动的必要条件。

此版不涉及未来的 B 站歌单/收藏夹 API，也不修改生产服务的部署路径。
