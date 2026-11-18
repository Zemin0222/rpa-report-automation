# 技术架构与关键设计决策 (Architecture & Engineering Trade-offs)

本文记录 `rpa-report-automation` 从单一写死脚本演进为「多场景插件引擎 + 可观测 AI 网关 + 公网演示沙箱」过程中做出的 5 项核心架构决策及工程取舍。

---

## 1. 为什么将主流程重构为「通用执行器 + `Workflow` Protocol 插件」？

### 背景与问题
项目初期只有一条 `core/pipeline.py` 流水线，把「CRM/广告/财务采集 → ETL 三方对账 → Excel/Word 出表 → OA 填报 → Webhook 推送」与「SQLite 幂等锁、30 分钟僵死锁回收、步骤审计、失败自动截图、热备份、存储清理」耦合在同一个函数里。引入第二个旗舰场景（**发票智能识别与自动报销**）时，如果直接复制粘贴 `pipeline.py`，幂等校验、防并发与直播回放逻辑将瞬间出现两套副本。

### 设计方案
1. **定义 `Workflow` 协议（[workflows/base.py](../workflows/base.py)）**：采用 `typing.Protocol` 结构化子类型约定四个正交生命周期：`collect(ctx) -> CollectedData`、`process(ctx, data) -> ProcessResult`、`deliver(ctx, result) -> DeliveryResult` 以及 `kpi_view(result) -> dict`，并通过 `requires_browser` 属性按需决定是否启动 Playwright。
2. **抽离通用执行器（[core/runner.py](../core/runner.py)）**：`run_workflow` / `run_backfill_workflow` 统一托管分布式锁替代逻辑（SQLite running lock）、每日日期幂等、步骤计时落库、异常捕获与自动截图告警、数据库在线热备和存储过期清理。
3. **注册中心与门面兼容（[workflows/registry.py](../workflows/registry.py)、[core/pipeline.py](../core/pipeline.py)）**：
   - `config/jobs.yaml` 每个 job 通过 `workflow:` 声明目标插件，`web_console.py` 从注册中心动态下发流程列表与总览 `view_type`。
   - `core/pipeline.py` 保留原有 `run_daily_pipeline()` 与 `run_backfill_pipeline()` 函数签名作为门面入口，确保历史 16 项自动化测试**不改一行断言**全部通过，证明行为 100% 无损迁移。

---

## 2. AI 能力为何采用「统一网关 + 语义缓存 + 预算熔断 + 规则兜底」？

### 背景与问题
在真实办公自动化中，大模型具备极强的非结构化理解与归因优势（跨版式电子发票字段识别、渠道波动解释、对账差异根因排查），但直接把 `OpenAI(...)` 散落在业务模块有四大隐患：
1. 公网在线演示如果不配置昂贵密钥，或遇到 API 限流/超时，流程不能直接崩溃；
2. 同样的发票附件或相似上下文反复重跑会重复烧 Token；
3. 大模型偶尔输出 Markdown 包裹的非规范 JSON 或缺字段导致下游报错；
4. 缺乏全局单日成本熔断。

### 设计方案（[ai/gateway.py](../ai/gateway.py) & [ai/schemas.py](../ai/schemas.py)）
每次 AI 任务调用统一经过 `AiGateway.complete_json(...)` 的四级漏斗：
1. **SHA256 语义缓存命中（`source="cache"`）**：将 `task + model + prompt + image_sha256` 拼装为缓存键查表 `ai_cache`，命中即零延迟直接校验返回。
2. **可用性与每日预算检查**：当 `llm.enabled=false`、`OPENAI_API_KEY` 未配置、或 `ai_usage` 表统计当日调用量超过 `daily_call_budget`（默认 `60` 次） / `daily_token_budget`（默认 `80,000` tokens）时，毫秒级无阻塞切换至确定性规则引擎。
3. **Pydantic Schema 验证与自修复重试**：对 `InvoiceExtractionSchema`、`ChannelAnomalyExplanation`、`ReconcileAttribution`、`DailyExecutiveAdvice` 执行严格类型校验；若首次 JSON 解析或校验失败，自动把错误信息附回对话历史再重试修复 `max_retries` 次（默认 1 次）。
4. **确定性规则兜底（`source="rule"`）**：若校验或网络最终失败，无缝回退至高质量静态模板/正则引擎（如 `pdfplumber` 正则 + 中文发票结构规则、基于 ROI/退款率/日环比阈值的归因规则）。所有结果统一携带 `source` (`ai` | `cache` | `rule`) 并在工作台 UI 透明标注来源与耗时。

---

## 3. 为什么选用 SQLite WAL + 进程内 `SingleWorkerQueue`，而不是上 Postgres + Redis + Celery？

### 场景定位决定技术栈
本系统定位在 **1~5 人小团队日常 RPA 与单机 2 核 4G 容器部署**。引入 Postgres、Redis 与 Celery 会增加 3 个重型基础设施进程与序列化运维成本，却不会带来实质吞吐收益——因为真正的计算瓶颈在于 **Chromium 浏览器实例**。

### 关键实现与边界
- **为什么必须单工位串行（[core/job_queue.py](../core/job_queue.py)）？** 单次带高亮直播的 Playwright 任务占用 300~500MB 内存，耗时约 20 秒。若允许公网多个访客并发启动浏览器，2C4G 云主机将在瞬间 OOM。`SingleWorkerQueue` 利用 `threading` + `queue.Queue(maxsize=10)` 保证全站同一时刻至多运行 **1 个机器人实例**，并依据历史执行平滑均值实时估算 `ahead_count` 与 `estimated_wait_sec`。
- **消除“排队干等”的体验优化**：访客触发 `/api/run-visual` 若发现前方有人占用工位，后端返回 `status="QUEUED"` 的同时附带最近一次成功完成的完整帧序列（`fallback_session`）；前端先展示排队横幅并**立即播放历史录像**，一旦轮到本人执行（状态切为 `RUNNING`），自动无缝接管真实浏览器操作直播画面。
- **数据层一致性**：SQLite 开启 `WAL` 模式，配合业务层 30 分钟运行锁超时自动回收与每次跑批后的 `sqlite3.Connection.backup` 热备份（保留最近 7 份），完全满足单机控制台写、调度器读写并发要求。

---

## 4. 公网在线演示如何实现「访客免登录体验」且兼顾安全隔离？

为了做到**简历/面试打开 URL 零门槛直接观看演示**，同时杜绝接口被恶意刷爆或误触发真实外发告警，在 `DEMO_MODE=1` 时启用 [app/demo_guard.py](../app/demo_guard.py)：

| 安全防线 | 实现机制 |
|---|---|
| **读写与运维隔离** | 游客可无密浏览所有总览、图表、下载、历史录像及触发演示；清理文件、DB 备份、手动补齐 7 天、发送测试告警及文件上传/删除在未解锁时统一拦截返回 **HTTP 403**。 |
| **强制本地预览模式** | 只要 `DEMO_MODE=1` 且请求方不是已认证管理员，后端无条件覆写 `dry_run=True`（并在前端锁定勾选框），确保即使用 `curl` 篡改请求也不会调用飞书/钉钉/SMTP 真实通道。 |
| **反代穿透 IP 滑窗限流** | 依序提取 `CF-Connecting-IP`、`X-Forwarded-For` 第一跳与 `X-Real-IP`，每个 IP 每 10 分钟最多触发 2 次演示运行；超出返回 **HTTP 429 + `Retry-After`**。 |
| **管理员 Fail-Closed 解锁** | 开启 `DEMO_MODE=1` 后若未显式设置 `DASHBOARD_PASSWORD`，`/api/login` 拒绝颁发管理 Cookie；设置口令后可通过右上角「管理员解锁」使用恒定时间比对验证进入全权限模式。 |
| **凌晨 03:30 自动洗库重置** | 调度器每日凌晨自动清空运行记录表、下载与截图等事务文件（保留 `auth_states` 与 `ai_cache`），重新回填近 7 天对账数据与 6 张演示发票，保证次日演示状态始终整洁。 |

---

## 5. 跨平台 PDF 电子发票解析与中文字形兼容实践

在落地 `expense_reimburse` 场景（[invoice_factory.py](../mock_systems/invoice_factory.py) & [invoice_extractor.py](../processors/invoice_extractor.py)）时遇到了两个典型生产环境差异问题：
1. **无系统字体环境下 PDF 生成退化**：Windows 上有 `msyh.ttc`（微软雅黑），Linux 容器安装 `fonts-noto-cjk`，但 GitHub Actions 原生 `ubuntu-latest` 无中文字体。因此封装 `resolve_cjk_font_file()`：优先匹配系统/容器本地中文字体以呈现最佳排版；在精简 CI runner 上自动回退至 PyMuPDF 内置 CJK 字体 `"china-s"`，避免报销生成器报错。
2. **Noto Sans CJK 抽取文字中的 Unicode 兼容表意文字（CJK Compatibility Ideographs）**：在 Linux Docker 中使用 `NotoSansCJK` 渲染发票并用 `pdfplumber` 提取文字时，“金”“辰”“行”等字会被映射为 `U+F90A`、`U+F971`、`U+FA08` 等兼容区码位，连字号也会成为 `U+2011`（Non-Breaking Hyphen），直接导致正则表达式匹配企业名称和开票日期失败。在 `parse_invoice_text()` 入口统一执行 `unicodedata.normalize("NFKC", text)` 后，所有环境下的字段与税号提取率恢复至 100%。
