# 升级与回滚验收

**合并 PR 不等于生产部署。** 先独立测试新提交，在明确维护窗口内排空场次与后台制作任务，得到部署批准之后再动生产。

## 1. 升级前的只读检查

在生产机器确认当前提交、服务状态和数据位置（后续操作必须使用实际配置的路径，而不是照搬示例）：

```sh
git rev-parse HEAD
systemctl --user status vc-ktv --no-pager
systemctl --user show vc-ktv -p WorkingDirectory,Environment,FragmentPath
# 按当前 systemd unit 实际监听地址/端口调整；这里是默认地址
curl -fsS http://127.0.0.1:8778/api/health
```

用管理员 UI 查看活动场次、正在播放的歌曲、正在下载和对齐的任务；未确认无在途任务时不要停止服务或复制媒体。CLI 只读检查无法证明各 worker 已排空。单独保存当前提交 SHA、unit 配置与环境变量（**包含私密信息，不要提交 Git**），记录可回滚的旧版本。

## 2. 服务完全停止后制作一致性快照

得到维护窗口批准、确认所有播放和制作任务结束之后，停止**全部**使用相同数据目录的服务实例。下面的命令需要人工确认；脚本本身不会自动停机：

```sh
systemctl --user stop vc-ktv
# 另行确认没有其它进程继续读写同一数据库/场次媒体

mkdir -p -m 700 "$HOME/vc-ktv-private-backups"
uv run --no-sync python -m scripts.offline_backup snapshot \
  --data "$HOME/.local/share/vc-ktv" \
  --config "$HOME/.config/vc-ktv" \
  --out "$HOME/vc-ktv-private-backups/pre-upgrade" \
  --service-stopped
uv run --no-sync python -m scripts.offline_backup verify \
  --snapshot "$HOME/vc-ktv-private-backups/pre-upgrade"
```

必须使用**尚不存在的快照目录**，重复执行需另选新名字。备份使用 SQLite Backup API 复制每个旧版或分场次 `room.sqlite3`（包含已提交 WAL 事务），并保存所有场次文件、媒体、活动场次指针和整个私密配置目录；对文件做 SHA-256、SQLite `quick_check`、活动指针检查。备份生成期间一旦失败，不发布不完整的快照。

**明确排除 `data/pipeline/` 的模型、源码及 CUDA 运行时**；升级或回滚时这些外部资产仍需独立保存和验证。其它数据目录（包括未被识别的目录）默认纳入备份。活动媒体硬链接存在、正在运行的 MediaPins owner lock、数据中存在符号链接、配置缺密钥时均拒绝创建备份。旧服务可能没有 owner lock，**`--service-stopped` 是操作者确认，不是自动停机或绝对防竞态机制**。不要在运行中强制备份。

快照内含账号哈希、场次信息、私密密钥和用户媒体；只存储于权限受控的私有空间，不上传到 GitHub、工单或公开对象存储。

## 3. 只在隔离副本上验收恢复与回滚

恢复操作仅允许使用**尚不存在的目标数据目录和配置目录**，遇到任何已有文件会拒绝覆盖：

```sh
uv run --no-sync python -m scripts.offline_backup restore \
  --snapshot "$HOME/vc-ktv-private-backups/pre-upgrade" \
  --data /absolute/private-test-dir/restored-data \
  --config /absolute/private-test-dir/restored-config
```

再使用单独的随机本地端口与 `VC_KTV_AUTOPREPARE=0`，分别通过新提交和原生产提交检查恢复场次、权限、歌单和可下载媒体。**不要把隔离恢复目录指向生产或与生产服务共用一个数据目录。** 恢复与旧版本兼容性通过后才继续升级；如恢复失败，保留旧版本并停止发布。恢复目录不含模型资产，GPU 功能需要另行挂载和验收。

## 4. 生产切换和回滚

需分别获得升级批准。保持所有 worker/播放器退出后更新生产 checkout，验证新提交与配置兼容，再启动并检查健康状态、管理员登录、场次、用户点歌、TV 播放和下载。任何异常立即停止**新版本**，确认没有新的生产写入之后回退旧提交；需要恢复数据时使用上面验证过的备份，**不要在运行中的目录上直接覆盖 SQLite/媒体**。如果新版本已经产生新的用户操作，则旧快照不包含这些新操作，应先明确数据保留决策，不能静默丢弃。

最后记录实际部署 SHA、CI、媒体与 GPU 验收、回滚演练结果，以及当前服务的 `systemctl --user is-enabled vc-ktv` 策略；`active` 不等于重启后自动启动。未验证的直播/CDN/长歌曲或不同 GPU 环境不要写成已通过。
