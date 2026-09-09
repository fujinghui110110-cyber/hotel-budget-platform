# Cloudflare Tunnel 示例

这只是稳定域名接入示例，不会创建、启动或验证任何公网隧道。正式使用前，用户需要自行确认并提供：自有域名、Cloudflare 账号、域名 DNS 管理权限和命名 Tunnel 凭据保存位置。

推荐让 macOS 上的本机 Web 仅监听 `127.0.0.1:8768`，由命名 Tunnel 转发到本机。这样数据库、上传原件、重算副本和审计记录继续留在 Mac 的受控数据目录。不要把 `.cloudflared/*.json` 凭据加入 Git，也不要把真实业务文件放进仓库或镜像。

准备步骤由用户在确认域名和账号后自行执行：

1. 安装并登录 `cloudflared`，创建命名 Tunnel，并将 DNS 主机名指向该 Tunnel。
2. 将本目录的 `config.yml.example` 复制到用户自己的 `~/.cloudflared/config.yml`，替换 UUID、凭据路径和 hostname。
3. 先在 Mac 本机用 `curl -fsS http://127.0.0.1:8768/healthz` 检查 Web，再运行 `cloudflared tunnel --config ~/.cloudflared/config.yml run`。外部访问由 Cloudflare 提供 HTTPS，Tunnel 到 Mac 的回源链路仍是本机 HTTP。
4. 经授权后再把同一命令配置为 launchd 常驻任务；首次上线前完成登录、项目隔离、上传处理、备份和 HTTPS 验收。应用配置届时应开启 `TRUST_PROXY=1`、`SECURE_SSL_REDIRECT=1`、`SESSION_COOKIE_SECURE=1` 和 `CSRF_COOKIE_SECURE=1`，并填写实际的 `DJANGO_ALLOWED_HOSTS` 与 `CSRF_TRUSTED_ORIGINS`。

当前部署包不启动 Quick Tunnel、命名 Tunnel 或任何外部发布动作。
