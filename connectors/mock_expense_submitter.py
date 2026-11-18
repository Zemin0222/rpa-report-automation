from __future__ import annotations

from pathlib import Path

from core.browser import BrowserManager
from core.config_loader import AppConfig
from processors.expense_rules import ExpenseAuditResult


class MockExpenseSubmitter:
    """Log into the corporate expense portal, fill each approved invoice row, attach PDFs, and submit."""

    def __init__(self, cfg: AppConfig, browser_mgr: BrowserManager) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr

    def submit(self, biz_date: str, audit: ExpenseAuditResult) -> str:
        if not audit.approved_items:
            return ""

        base_url = self.cfg.connectors.mock_expense.base_url.rstrip("/")
        submit_url = f"{base_url}/expense/submit?date={biz_date}"
        username, password = self.cfg.connectors.mock_expense.resolve_credentials()
        phase = "自动报销填单"

        with self.browser_mgr.open_context("mock_expense") as (context, page):
            with self.browser_mgr.capture_on_error(page, "mock_expense_submit"):
                self.browser_mgr.step_goto(page, submit_url, phase, "打开企业财务费用报销系统")
                if "/expense/login" in page.url:
                    self.browser_mgr.step_fill(
                        page,
                        'input[name="username"]',
                        username,
                        phase,
                        f"填入报销系统账号 {username}",
                    )
                    self.browser_mgr.step_fill(
                        page,
                        'input[name="password"]',
                        password,
                        phase,
                        "填入报销系统密码",
                        mask=True,
                    )
                    self.browser_mgr.step_click(page, "#expense-login-btn", phase, "点击登录报销平台")
                    page.wait_for_load_state("domcontentloaded")
                    self.browser_mgr.save_state(context, "mock_expense")
                    self.browser_mgr.step_goto(page, submit_url, phase, "进入新建费用报销申请单")

                self.browser_mgr.step_fill(page, "#exp-biz-date", biz_date, phase, f"填写报销日期 {biz_date}")
                self.browser_mgr.step_fill(
                    page,
                    "#exp-applicant",
                    self.cfg.expense.applicant_name,
                    phase,
                    f"填写申报人: {self.cfg.expense.applicant_name}",
                )
                self.browser_mgr.step_fill(
                    page,
                    "#exp-department",
                    self.cfg.expense.department,
                    phase,
                    f"填写成本中心: {self.cfg.expense.department}",
                )
                remark = (
                    f"合规通过 {audit.approved_count} 张发票（合计 ¥{audit.approved_amount:,.2f}），"
                    f"拦截 {audit.flagged_count} 张风险发票。"
                )
                self.browser_mgr.step_fill(page, "#exp-remark", remark, phase, "填写风控审查附注说明")

                for idx, item in enumerate(audit.approved_items):
                    rec = item.record
                    if idx > 0:
                        self.browser_mgr.step_click(
                            page,
                            "#exp-add-row-btn",
                            phase,
                            f"点击新增第 {idx + 1} 行报销明细",
                        )
                    self.browser_mgr.step_select(
                        page,
                        f"#exp-cat-{idx}",
                        rec.category,
                        phase,
                        f"选择第 {idx + 1} 张发票类别: {rec.category}",
                    )
                    self.browser_mgr.step_fill(
                        page,
                        f"#exp-inv-{idx}",
                        rec.invoice_number,
                        phase,
                        f"填入发票号码 {rec.invoice_number}",
                    )
                    self.browser_mgr.step_fill(
                        page,
                        f"#exp-vendor-{idx}",
                        rec.seller_name,
                        phase,
                        f"填入销售方: {rec.seller_name}",
                    )
                    self.browser_mgr.step_fill(
                        page,
                        f"#exp-amt-{idx}",
                        f"{rec.total_amount:.2f}",
                        phase,
                        f"填入含税报销金额 ¥{rec.total_amount:.2f}",
                    )
                    self.browser_mgr.step_upload(
                        page,
                        f"#exp-file-{idx}",
                        Path(rec.file_path),
                        phase,
                        f"上传发票原件 {rec.file_name}",
                    )

                self.browser_mgr.step_click(
                    page,
                    "#exp-submit-btn",
                    phase,
                    f"提交报销申请单（共 {audit.approved_count} 张，¥{audit.approved_amount:,.2f}）",
                )
                page.wait_for_selector("#exp-ticket-id")
                ticket_id = page.locator("#exp-ticket-id").inner_text().strip()
                self.browser_mgr.step_snap(
                    page,
                    phase,
                    "SUBMIT",
                    "#exp-ticket-id",
                    f"报销单提交成功，财务审批单号: {ticket_id}",
                )
                return ticket_id
