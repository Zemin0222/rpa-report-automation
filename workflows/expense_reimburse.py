from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from connectors.mock_expense_submitter import MockExpenseSubmitter
from core.config_loader import AppConfig, JobDefinition
from mock_systems.invoice_factory import populate_sample_invoices
from notifiers.webhook import send_summary_card
from processors.expense_rules import ExpenseAuditResult, audit_invoices
from processors.invoice_extractor import InvoiceRecord, extract_invoice_from_file
from reporters.expense_reporter import generate_expense_excel_report
from workflows.base import (
    CollectedData,
    DeliveryResult,
    ProcessResult,
    RunContext,
)

_ALLOWED_INVOICE_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}


class ExpenseReimburseWorkflow:
    """Invoice extraction + compliance auditing + Playwright automated expense submission."""

    key = "expense_reimburse"
    title = "发票智能识别与自动报销"
    description = "解析电子发票（PDF/图片），完成重复/超标/抬头合规检查，并登录报销系统逐单填报与上传附件"
    view_type = "expense"

    def needs_browser(
        self,
        cfg: AppConfig,
        job_def: JobDefinition,
        simulate_failure: bool = False,
    ) -> bool:
        return bool(simulate_failure or cfg.connectors.mock_expense.enabled)

    def run_failure_drill(self, ctx: RunContext) -> None:
        if ctx.browser_mgr is None:
            raise RuntimeError("Failure drill requires an active BrowserManager")
        base_url = ctx.cfg.connectors.mock_expense.base_url.rstrip("/")
        username, password = ctx.cfg.connectors.mock_expense.resolve_credentials()
        with ctx.browser_mgr.open_context("mock_expense") as (context, page):
            with ctx.browser_mgr.capture_on_error(page, "drill_expense_redesign"):
                ctx.browser_mgr.step_goto(
                    page,
                    f"{base_url}/expense/submit?date={ctx.biz_date}",
                    "故障演练",
                    "打开报销系统填报页面",
                )
                if "/expense/login" in page.url:
                    ctx.browser_mgr.step_fill(
                        page,
                        'input[name="username"]',
                        username,
                        "故障演练",
                        f"输入账号 {username}",
                    )
                    ctx.browser_mgr.step_fill(
                        page,
                        'input[name="password"]',
                        password,
                        "故障演练",
                        "输入密码",
                        mask=True,
                    )
                    ctx.browser_mgr.step_click(page, "#expense-login-btn", "故障演练", "点击登录")
                    page.wait_for_load_state("domcontentloaded")
                    ctx.browser_mgr.save_state(context, "mock_expense")
                    ctx.browser_mgr.step_goto(
                        page,
                        f"{base_url}/expense/submit?date={ctx.biz_date}",
                        "故障演练",
                        "进入报销填报页",
                    )
                page.wait_for_selector("#redesigned-expense-v2-btn", timeout=1200)

    def collect(self, ctx: RunContext) -> CollectedData:
        t0 = time.perf_counter()
        invoice_dir = ctx.cfg.storage.invoices
        assert invoice_dir is not None
        invoice_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = invoice_dir / ".thumbs"

        candidate_files = sorted(
            p
            for p in invoice_dir.iterdir()
            if p.is_file() and p.suffix.lower() in _ALLOWED_INVOICE_EXTS and not p.name.startswith(".")
        )
        if not candidate_files:
            candidate_files = populate_sample_invoices(
                invoice_dir,
                ctx.biz_date,
                ctx.cfg.expense.company_name,
                ctx.cfg.expense.company_tax_id,
                overwrite=False,
            )

        records: list[InvoiceRecord] = [
            extract_invoice_from_file(p, llm_cfg=ctx.cfg.llm, thumb_dir=thumb_dir)
            for p in candidate_files
        ]

        ctx.record_live(
            "发票读取与结构化抽取",
            "READ",
            f"{len(records)} 张电子发票",
            f"从发票收件箱读取 {len(records)} 份文件，完成票号/抬头/税号/类别/金额抽取",
        )
        ctx.log_step(
            "collect_invoices",
            "DONE",
            f"files={len(records)}",
            duration_ms=(time.perf_counter() - t0) * 1000,
        )
        return CollectedData(row_count=len(records), payload=records)

    def process(self, ctx: RunContext, data: CollectedData) -> ProcessResult:
        t0 = time.perf_counter()
        records: list[InvoiceRecord] = list(data.payload or [])
        audit = audit_invoices(records, ctx.biz_date, ctx.cfg.expense)

        ctx.record_live(
            "合规审核与风控拦截",
            "COMPUTE",
            "expense_rules",
            (
                f"审核完成：{audit.approved_count} 张合规待提交（¥{audit.approved_amount:,.2f}），"
                f"{audit.flagged_count} 张因重复/超标/抬头不符被拦截"
            ),
        )
        ctx.log_step(
            "audit_invoices",
            "DONE",
            (
                f"total={audit.total_count}, approved={audit.approved_count}, "
                f"flagged={audit.flagged_count}"
            ),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

        return ProcessResult(
            row_count=audit.total_count,
            anomaly_count=audit.flagged_count,
            reconcile_diff_count=0,
            summary_text=audit.summary_text,
            kpis=audit.kpis,
            payload=audit,
        )

    def deliver(self, ctx: RunContext, result: ProcessResult) -> DeliveryResult:
        audit: ExpenseAuditResult = result.payload
        ticket_id = ""

        if audit.approved_items and ctx.cfg.connectors.mock_expense.enabled and ctx.browser_mgr is not None:
            t_sub = time.perf_counter()
            ticket_id = MockExpenseSubmitter(ctx.cfg, ctx.browser_mgr).submit(ctx.biz_date, audit)
            audit.kpis["expense_ticket_id"] = ticket_id
            cards = audit.kpis.get("cards") or []
            if len(cards) >= 5 and ticket_id:
                cards[4]["value"] = ticket_id
                cards[4]["sub"] = f"已自动上传 {audit.approved_count} 份发票附件"
                cards[4]["tone"] = "ok"
            ctx.log_step(
                "submit_expense_claim",
                "DONE",
                f"ticket={ticket_id}, approved={audit.approved_count}",
                duration_ms=(time.perf_counter() - t_sub) * 1000,
            )

        t_rep = time.perf_counter()
        excel_path = generate_expense_excel_report(audit, ctx.cfg.storage.outputs, ticket_id=ticket_id)
        ctx.record_live(
            "生成报销台账",
            "RENDER",
            excel_path.name,
            f"已导出 3 Sheet 报销合规审查台账 {excel_path.name}",
        )
        ctx.log_step(
            "render_expense_report",
            "DONE",
            excel_path.name,
            duration_ms=(time.perf_counter() - t_rep) * 1000,
        )

        t_notify = time.perf_counter()
        card_title = f"{ctx.biz_date} 自动报销审查简报 | 回执 {ticket_id or '无合规票据'}"
        md_body = (
            f"**审验总数**: {audit.total_count} 张 (¥{audit.total_amount:,.2f})\n"
            f"**合规自动提交**: {audit.approved_count} 张 (¥{audit.approved_amount:,.2f})\n"
            f"**规则拦截**: {audit.flagged_count} 张 (¥{audit.flagged_amount:,.2f})\n"
            f"**报销单回执**: `{ticket_id or '未提交'}`\n"
            f"**台账附件**: `{excel_path.name}`\n\n"
            f"{audit.summary_text}"
        )
        notify_preview = send_summary_card(
            ctx.cfg.notifier,
            title=card_title,
            markdown_body=md_body,
            output_dir=ctx.cfg.storage.outputs,
            preview_filename=f"expense_notify_{ctx.biz_date}.json",
            is_warning=audit.flagged_count > 0,
            force_dry_run=ctx.dry_run,
        )
        ctx.record_live(
            "通知分发",
            "NOTIFY",
            notify_preview.name,
            f"已生成报销通知卡片 {notify_preview.name}",
        )
        ctx.log_step(
            "notify",
            "DONE",
            notify_preview.name,
            duration_ms=(time.perf_counter() - t_notify) * 1000,
        )

        return DeliveryResult(
            oa_ticket_id=ticket_id or None,
            excel_path=excel_path,
            word_path=None,
            notify_preview_path=notify_preview,
        )

    def kpi_view(self, result: ProcessResult) -> dict[str, Any]:
        return dict(result.kpis)
