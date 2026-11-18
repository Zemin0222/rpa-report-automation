from __future__ import annotations

from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential

from core.browser import BrowserManager
from core.config_loader import AppConfig
from core.logger import get_logger
from processors.etl import EtlResult

logger = get_logger("rpa.connector.oa_submitter")


class MockOaSubmitter:
    """Opens the OA approval form, fills KPIs/anomalies, uploads Excel report, and submits."""

    site_key = "mock_oa"

    def __init__(self, cfg: AppConfig, browser_mgr: BrowserManager) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def submit(
        self,
        biz_date: str,
        etl: EtlResult,
        summary_text: str,
        excel_path: Path,
    ) -> str:
        conn_cfg = self.cfg.connectors.mock_oa
        base_url = conn_cfg.base_url.rstrip("/")
        username, password = conn_cfg.resolve_credentials()
        phase = "OA 报备填单"

        k = etl.kpis
        anom_cnt = int(k.get("anomaly_count", 0))
        diff_cnt = int((k.get("reconcile_summary") or {}).get("discrepancy_count", 0))
        risk_level = "高危异常" if (anom_cnt >= 3 or diff_cnt >= 4) else ("关注" if (anom_cnt > 0 or diff_cnt > 0) else "正常")

        with self.browser_mgr.open_context(self.site_key) as (context, page):
            with self.browser_mgr.capture_on_error(page, "oa_submit"):
                submit_url = f"{base_url}/oa/submit?date={biz_date}"
                logger.info("Navigating to OA submit portal: %s", submit_url)
                self.browser_mgr.step_goto(page, submit_url, phase, f"打开 OA 日报审批填报单 ({biz_date})")

                if "/oa/login" in page.url:
                    logger.info("OA session missing or expired; signing in as %s", username)
                    self.browser_mgr.step_fill(page, 'input[name="username"]', username, phase, f"输入 OA 员工账号 {username}")
                    self.browser_mgr.step_fill(page, 'input[name="password"]', password, phase, "输入 OA 登录密码", mask=True)
                    self.browser_mgr.step_click(page, "#oa-login-btn", phase, "点击登录 OA 审批系统")
                    page.wait_for_load_state("domcontentloaded")
                    if "/oa/login" in page.url:
                        raise RuntimeError("OA login failed: invalid credentials or login redirect")
                    self.browser_mgr.save_state(context, self.site_key)
                    self.browser_mgr.step_goto(page, submit_url, phase, "进入 OA 经营日报填报表单")

                self.browser_mgr.step_fill(page, "#oa-biz-date", biz_date, phase, f"自动填写报告日期 {biz_date}")
                self.browser_mgr.step_fill(
                    page,
                    "#oa-total-orders",
                    str(k.get("total_orders", 0)),
                    phase,
                    f"自动填写成交订单数: {k.get('total_orders', 0)}",
                )
                self.browser_mgr.step_fill(
                    page,
                    "#oa-net-sales",
                    f"{float(k.get('total_net_sales', 0.0)):.2f}",
                    phase,
                    f"自动填写净销售额: ¥{float(k.get('total_net_sales', 0.0)):,.2f}",
                )
                self.browser_mgr.step_fill(
                    page,
                    "#oa-overall-roi",
                    f"{float(k.get('overall_roi', 0.0)):.2f}",
                    phase,
                    f"自动填写整体 ROI: {float(k.get('overall_roi', 0.0)):.2f}",
                )
                self.browser_mgr.step_select(page, "#oa-risk-level", risk_level, phase, f"自动判定风险等级: {risk_level}")
                self.browser_mgr.step_fill(
                    page,
                    "#oa-summary-note",
                    summary_text,
                    phase,
                    "自动填入经营摘要与三方对账结论",
                )
                self.browser_mgr.step_upload(
                    page,
                    "#oa-report-file",
                    excel_path,
                    phase,
                    f"自动上传 Excel 日报附件 ({excel_path.name})",
                )
                self.browser_mgr.step_click(page, "#oa-submit-btn", phase, "点击提交 OA 审批单")

                page.wait_for_selector("#oa-ticket-id")
                ticket_id = page.locator("#oa-ticket-id").inner_text().strip()
                self.browser_mgr.step_snap(
                    page,
                    phase,
                    "SUBMIT",
                    "#oa-ticket-id",
                    f"OA 审批单提交成功，回执单号: {ticket_id}",
                )
                logger.info("OA daily report submitted -> ticket_id=%s", ticket_id)
                return ticket_id
