# 跨系统办公数据搬运与自动化工作台 (RPA Ops Workbench)

[![CI](https://github.com/Zemin0222/rpa-report-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/Zemin0222/rpa-report-automation/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-0F52BA)
![License](https://img.shields.io/badge/License-MIT-0B5D3B)

基于 **Python 代码流（Playwright + FastAPI + Pandas + OpenPyXL + python-docx）** 构建的跨系统 RPA 自动化平台。自动完成多系统登录取数、CRM 与财务 PDF 三方对账、图文 Excel/Word 报表生成、OA 日报自动填报提交及群消息推送，并提供**实时操作直播视窗**，直观展示机器人每一步点击、填表、上传与回执提交过程。

---

## 核心能力

- **真实网页自动化（Playwright）**：支持登录态持久化（`storage_state`）、扫码/滑块人工辅助登录（`auth-login`）、表格导出下载、翻页抓取、SPA 接口拦截，以及失败自动全页截图与高亮标记操作动作。
- **跨系统汇总与三方对账**：统一渠道口径、订单去重、计算净销售额/退款率/广告 ROI/日环比，并与财务结算单 PDF 自动逐单比对（识别漏单、长款、金额不符）。
- **自动回填 OA 审批单**：根据当日异常渠道与对账差异自动判定风险等级，登录 OA 填报表单、填入经营摘要、上传 Excel 日报附件并获取回执单号。
- **可视化操作直播与回放**：内置 FastAPI 无刷新 Web 工作台（`http://127.0.0.1:8501`），可实时观看机器人每一步截图与动作指令流，并支持本地 `--watch` 有头浏览器慢放。
- **生产工程基线**：SQLite WAL 状态库、30 分钟并发僵死锁自动超时恢复、文件 SHA256 去重、在线热备份、`doctor` 环境自检、飞书/钉钉 HMAC-SHA256 加签与失败即时告警。

## 架构与模块划分

- `config/`：`jobs.yaml` 多任务与阈值配置、`.env.example` 环境变量模板
- `core/`：`config_loader.py`、`browser.py`（Playwright 会话复用与可视高亮）、`live_observer.py`、`pipeline.py`、`doctor.py`、`housekeeping.py`
- `connectors/`：插件化采集与填报器（`registry.py`、`mock_crm_connector.py`、`mock_ads_connector.py`、`mock_finance_connector.py`、`mock_oa_submitter.py`、`local_drop_connector.py`、`imap_mail_connector.py`、`generic_web_connector.py`、`template_connector.py`）
- `processors/`：`etl.py`（清洗聚合、异常检测、三方对账）、`summary_builder.py`（规则摘要 + 可选 OpenAI 兼容摘要）
- `reporters/`：`excel_reporter.py`（4 Sheet 样式化图表日报）、`word_reporter.py`（嵌入式趋势图周报简报）
- `notifiers/`：`webhook.py`（飞书/钉钉卡片与加签，默认本地 Dry-Run 预览）、`email_sender.py`（SMTP SSL 邮件）
- `app/`：`web_console.py` + `templates/console.html`（FastAPI SPA 运维工作台）
- `mock_systems/`：`server.py`（内置 CRM、广告投放、SPA 财务结算 PDF、OA 填报模拟系统）
- `storage/`：SQLite WAL 状态库、登录态 `auth_states/`、原始下载、产出报表 `outputs/`、故障截图 `screenshots/`、热备份 `backups/`
- `tests/`：覆盖 ETL 对账、状态库并发锁、IMAP/Dropzone、Webhook 加签、Web 控制台与直播帧流的全套 `pytest` 测试

---

## 本地快速启动

```bash
# 1. 安装依赖并下载 Chromium
pip install -r requirements.txt
playwright install chromium

# 2. 环境自检
python main.py doctor

# 3. 运行全量自动化测试
python -m pytest -v

# 4. 启动本地模拟系统（CRM / 广告 / 财务 / OA）
python main.py mock-server

# 5. 另开终端执行跑批或补跑历史数据
python main.py run-once --force --watch --fresh-login   # 桌面弹出 Chrome 慢放观看每一步
python main.py backfill --days 7                       # 补齐最近 7 天数据
python main.py run-once --simulate-failure             # 演练网页故障自动截图留痕

# 6. 启动 Web 操作工作台 (http://127.0.0.1:8501)
python main.py console --host 127.0.0.1 --port 8501
```

## Docker 一键运行

```bash
# 连同内置模拟业务系统一起启动，用于完整闭环体验
docker compose --profile demo up -d --build

# 在调度容器中触发一次端到端跑批
docker compose exec rpa-scheduler python main.py run-once --force
```

启动后访问 [http://127.0.0.1:8501](http://127.0.0.1:8501)，进入「机器人操作直播」页面并点击 **观看机器人实时操作**，即可实时查看机器人登录、取数、对账、生表与填写 OA 审批单的全流程。

生产部署与云服务器迁移说明见 [deploy/CLOUD_DEPLOY.md](deploy/CLOUD_DEPLOY.md)。
