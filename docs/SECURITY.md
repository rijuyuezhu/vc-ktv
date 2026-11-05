# 安全与网络

VC-KTV 适合在可信局域网或 Tailscale 中部署。默认监听 `127.0.0.1:8778`；如需通过反向代理开放访问，请使用 HTTPS。

管理员密码和签名密钥位于 `~/.config/vc-ktv/`；场次数据、媒体和网易云登录态位于 `~/.local/share/vc-ktv/`。这些目录不要上传到公开仓库。

通过受控代理转发时，可以设置 `VC_KTV_TRUSTED_PROXY_IPS` 为代理连接应用时的实际 IP，例如：

```sh
VC_KTV_TRUSTED_PROXY_IPS=127.0.0.1 uv run --no-sync python -m vc_ktv
```

代理需要覆盖转发的真实客户端地址头：

```nginx
proxy_set_header X-Real-IP $remote_addr;
```

请使用有权播放和处理的音视频素材，遵循平台、内容及模型的使用规则。
