# 部署与升级

以下命令适用于使用 `systemd --user` 管理服务的 Linux 机器。部署前在管理员页面确认没有歌曲正在播放或制作，检查实际服务工作目录和数据目录。

## 启动常驻服务

保存以下配置为 `~/.config/systemd/user/vc-ktv.service`，将路径替换为自己的仓库目录：

```ini
[Unit]
Description=VC-KTV
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/vc-ktv
ExecStart=/absolute/path/to/vc-ktv/.venv/bin/python -m vc_ktv
Environment=VC_KTV_AUTOPREPARE=0
Restart=on-failure
UMask=0077

[Install]
WantedBy=default.target
```

该示例默认关闭自动制作。准备好 CPU 或 GPU 制作环境后，可按 [安装指南](INSTALL.md) 调整服务环境变量。

```sh
systemctl --user daemon-reload
systemctl --user enable --now vc-ktv.service
systemctl --user status vc-ktv.service
curl -fsS http://127.0.0.1:8778/api/health
```

## 备份场次

停止使用同一数据目录的所有 VC-KTV 实例后，执行：

```sh
systemctl --user stop vc-ktv.service
uv run --no-sync python -m scripts.offline_backup snapshot \
  --data "$HOME/.local/share/vc-ktv" \
  --config "$HOME/.config/vc-ktv" \
  --out "$HOME/vc-ktv-backup" --service-stopped
uv run --no-sync python -m scripts.offline_backup verify \
  --snapshot "$HOME/vc-ktv-backup"
```

备份目录必须是新目录。模型资源独立存放，需要时单独备份。

## 更新和回滚

确认没有歌曲正在播放或制作，将 `COMMIT_SHA` 替换为已通过 CI 的目标提交，在部署工作树执行：

```sh
systemctl --user stop vc-ktv.service
git fetch origin
git switch --detach COMMIT_SHA
uv sync --locked --no-dev
systemctl --user start vc-ktv.service
curl -fsS http://127.0.0.1:8778/api/health
```

回滚时停止服务、切回原提交并重启。需要恢复场次数据时，使用 `python -m scripts.offline_backup restore --help` 查看恢复命令，并先在独立目录检查备份。
