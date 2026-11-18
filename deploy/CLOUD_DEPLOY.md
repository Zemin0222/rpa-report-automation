# 云服务器上线、公网演示部署与运维手册

支持两种上线模式：
1. **公网在线演示部署（`docker-compose.prod.yml` + `Caddy` 自动 HTTPS）**：免登录体验全工作台与直播机器人操作，自带单工位排队、IP 限流、管理操作只读锁定与凌晨数据重置，适合求职作品集 / 面试官在线访问（2 核 4G 云主机即可）。
2. **私有生产部署（`docker-compose.yml` + `Nginx`）**：对接真实内部 ERP/CRM/邮箱与群机器人。

---

## 一、公网在线演示模式一键部署（推荐：2 核 4G 云主机 + Caddy HTTPS）

### 1. 环境准备

- 推荐区域：**中国香港 / 新加坡 / 海外轻应用云主机**（2C4G Docker 环境），域名解析 A 记录指向主机公网 IP 后**无需等待 ICP 备案即可直接签发 HTTPS**。
- 防火墙/安全组开放端口：`80/tcp`、`443/tcp`（无需开放 `8501` 与 `8765` 到公网）。

### 2. 配置环境变量

```bash
cp config/.env.example config/.env
```

编辑 `config/.env`：

```dotenv
# 设为公网演示模式
DEMO_MODE=1
# 管理员解锁口令（控制面板右上角「管理员解锁」使用，务必修改为强口令）
DASHBOARD_PASSWORD=change_me_strong_password

# 限流策略（每 IP 每 10 分钟最多触发 2 次演示；全站同时仅运行 1 个机器人）
DEMO_RATE_LIMIT_MAX=2
DEMO_RATE_LIMIT_WINDOW_SEC=600
DEMO_MAX_QUEUE=10

# 填入已解析至本机的域名（自动申请 Let's Encrypt HTTPS）；暂未绑定域名可保持 :80
RPA_DOMAIN=rpa.yourdomain.com

# 可选大模型增强（推荐接入 DeepSeek / 通义千问；留空则自动切换确定性规则引擎兜底）
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

### 3. 启动生产演示栈并初始化近 7 天数据

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec rpa-scheduler python main.py doctor
docker compose -f docker-compose.prod.yml exec rpa-scheduler python main.py demo-reset --days 7
```

初始化完成后，打开 `https://rpa.yourdomain.com` 即可免密查看最近 7 天对账趋势、`expense_reimburse` 自动报销样本及触发机器人实时演示；调度器会在每日 `03:30` 自动清空演示增量数据并重建近 7 天基线。

### 4. 在线拨测监控（UptimeRobot 等）

- **Monitor Type**: HTTPS
- **URL**: `https://rpa.yourdomain.com/api/health`（期望返回 `{"status":"ok"}`）
- 建议巡检周期：5 分钟；异常时触发邮件通知。

---

## 二、私有生产模式（对接真实系统 + Nginx 备选）

### 1. 准备真实凭据与数据源

1. 在 `config/.env` 中保持 `DEMO_MODE=0`，填写真实：
   - `DASHBOARD_PASSWORD=<强口令>`
   - `FEISHU_WEBHOOK_URL` / `FEISHU_WEBHOOK_SECRET`（或 `DINGTALK_*`、`SMTP_*`）
   - 若开启邮箱附件自动采集：填写 `IMAP_HOST`、`IMAP_USER`、`IMAP_PASSWORD`
   - 若开启 AI 分析建议：填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`
2. 在 `config/jobs.yaml` 中：
   - 将 `notifier.dry_run` 设为 `false` 启用真实群推送；
   - 停用 `mock_*` 演示源，配置真实 Web 后台（`generic_web` 或自定义插件）、`imap_mail` 及 `local_drop`。

### 2. 本地扫码登录态同步（处理微信扫码 / 滑块验证）

```bash
python main.py auth-login --site my_erp --url https://erp.example.com/login
```

完成后将 `storage/auth_states/my_erp.json` 上传至云服务器项目的 `storage/auth_states/` 目录。

### 3. 启动私有容器与 Nginx HTTPS 反代

```bash
# 在 config/.env 设置仅本机回环绑定，避免裸露 8501 端口：
# DASHBOARD_BIND_IP=127.0.0.1
docker compose up -d --build
docker compose exec rpa-scheduler python main.py doctor
```

参考 [deploy/nginx.conf](nginx.conf) 配置 `/etc/nginx/sites-available/rpa-console.conf`，修改域名与证书路径后执行：

```bash
sudo ln -s /etc/nginx/sites-available/rpa-console.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## 三、备份、AI 预算与故障恢复

- 每次成功跑批后，系统自动执行 SQLite 在线备份至 `storage/backups/state_backup_*.db`（保留最近 7 份）。
- `config/jobs.yaml` 默认启用每日 AI 调用与 Token 上限保护（`daily_call_budget: 60`, `daily_token_budget: 80000`），超限或接口波动时无缝回退到规则引擎，工作台运行页可实时查看缓存命中率。
- 若目标站点改版导致跑批失败，可在「手工传单与截图」查看全页截图并人工触发重跑：

```bash
docker compose exec rpa-scheduler python main.py run-once --force
```
