# 云服务器生产上线与运维手册

## 1. 准备配置与密钥

1. 复制 `config/.env.example` 为 `config/.env` 并填写真实凭据：
   - 控制台公网防护：`DASHBOARD_PASSWORD=<强口令>`
   - 飞书/钉钉群通知与加签：`FEISHU_WEBHOOK_URL` + `FEISHU_WEBHOOK_SECRET`（或 `DINGTALK_*`）
   - 若开启邮箱附件自动采集：填写 `IMAP_HOST`、`IMAP_USER`、`IMAP_PASSWORD`
   - 若开启 AI 经营结论：填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`
2. 在 `config/jobs.yaml` 中：
   - 将 `notifier.dry_run` 设为 `false` 启用真实群推送；
   - 在 `jobs[].sources` 中停用不需要的 `mock_*` 演示源，配置真实 Web 后台（`generic_web` 或自定义插件）、`imap_mail` 及 `local_drop`。

## 2. 本地扫码登录态同步（处理微信扫码 / 滑块验证）

云服务器无头环境无法人工扫码时，先在本地电脑执行：

```bash
python main.py auth-login --site my_erp --url https://erp.example.com/login
```

完成后将生成的 `storage/auth_states/my_erp.json` 上传至云服务器项目的 `storage/auth_states/` 目录。

## 3. 安全启动容器（生产模式）

若控制台仅允许本机 Nginx 反代访问（不直接裸露 `8501` 端口到公网），在 `.env` 中设置：

```bash
DASHBOARD_BIND_IP=127.0.0.1
```

随后启动服务（不加载 `--profile demo` 靶场）：

```bash
docker compose up -d --build
docker compose exec rpa-scheduler python main.py doctor
```

## 4. 配置 Nginx HTTPS 反代（推荐）

将 `deploy/nginx.conf` 复制至 `/etc/nginx/sites-available/rpa-console.conf`，替换域名与 SSL 证书路径后重载 Nginx：

```bash
sudo ln -s /etc/nginx/sites-available/rpa-console.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 5. 数据备份与故障恢复

- 每次成功跑批后，系统会自动调用 SQLite 在线备份至 `storage/backups/state_backup_*.db`（默认保留最近 7 份）。
- 若遇站点改版导致跑批失败，系统会自动保存全页截图至 `storage/screenshots/` 并发送红色故障告警卡片；恢复时只需同步新登录态或更新选择器后执行：

```bash
docker compose exec rpa-scheduler python main.py run-once --force
```
