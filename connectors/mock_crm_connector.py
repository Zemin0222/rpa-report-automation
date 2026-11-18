from __future__ import annotations

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from connectors.base import BaseConnector, CollectResult
from core.browser import BrowserManager
from core.config_loader import AppConfig
from core.logger import get_logger

logger = get_logger("rpa.connector.crm")


class MockCrmConnector(BaseConnector):
    """Logs into the CRM portal via Playwright, filters by date, and exports order CSV."""

    name = "mock_crm"
    site_key = "mock_crm"

    def __init__(self, cfg: AppConfig, browser_mgr: BrowserManager) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def fetch(self, biz_date: str) -> CollectResult:
        conn_cfg = self.cfg.connectors.mock_crm
        base_url = conn_cfg.base_url.rstrip("/")
        username, password = conn_cfg.resolve_credentials()

        download_dir = self.cfg.storage.downloads / biz_date
        download_dir.mkdir(parents=True, exist_ok=True)
        target_csv = download_dir / f"crm_orders_{biz_date}.csv"
        phase = "CRM 订单后台"

        with self.browser_mgr.open_context(self.site_key) as (context, page):
            with self.browser_mgr.capture_on_error(page, "crm_collect"):
                orders_url = f"{base_url}/crm/orders?date={biz_date}"
                logger.info("Navigating to CRM orders page: %s", orders_url)
                self.browser_mgr.step_goto(page, orders_url, phase, f"打开 CRM 订单页 ({biz_date})")

                if "/crm/login" in page.url:
                    logger.info("CRM session missing or expired; signing in as %s", username)
                    self.browser_mgr.step_fill(page, 'input[name="username"]', username, phase, f"输入 CRM 账号 {username}")
                    self.browser_mgr.step_fill(page, 'input[name="password"]', password, phase, "输入 CRM 登录密码", mask=True)
                    self.browser_mgr.step_click(page, "#crm-login-btn", phase, "点击登录 CRM 系统")
                    page.wait_for_load_state("domcontentloaded")
                    if "/crm/login" in page.url:
                        raise RuntimeError("CRM login failed: invalid credentials or login redirect")
                    self.browser_mgr.save_state(context, self.site_key)
                    self.browser_mgr.step_goto(page, orders_url, phase, f"进入 {biz_date} 订单列表")

                page.wait_for_selector("#export-csv-btn")
                self.browser_mgr.step_snap(page, phase, "READ", "#crm-orders-table", "核验当日订单列表")
                with page.expect_download() as download_info:
                    self.browser_mgr.step_click(page, "#export-csv-btn", phase, "点击导出当日订单 CSV")
                download = download_info.value
                download.save_as(str(target_csv))

        df = pd.read_csv(target_csv, encoding="utf-8-sig")
        self.browser_mgr.emit(phase, "DOWNLOAD", target_csv.name, f"已保存 {len(df)} 条 CRM 订单明细")
        logger.info("CRM export collected %d rows -> %s", len(df), target_csv.name)
        return CollectResult(
            source_name=self.name,
            biz_date=biz_date,
            dataframe=df,
            raw_file_path=target_csv,
            row_count=len(df),
        )
