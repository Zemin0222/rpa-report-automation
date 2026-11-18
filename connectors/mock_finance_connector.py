from __future__ import annotations

from pathlib import Path
import pandas as pd
import pdfplumber
from tenacity import retry, stop_after_attempt, wait_exponential

from connectors.base import BaseConnector, CollectResult
from core.browser import BrowserManager
from core.config_loader import AppConfig
from core.logger import get_logger

logger = get_logger("rpa.connector.finance")


class MockFinanceConnector(BaseConnector):
    """Logs into SPA Finance portal, intercepts XHR JSON, polls async PDF export, and parses PDF."""

    name = "mock_finance"
    site_key = "mock_finance"

    def __init__(self, cfg: AppConfig, browser_mgr: BrowserManager) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr

    @staticmethod
    def _parse_settlement_pdf(pdf_path: Path) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if len(table) >= 2 and table[0]:
                        headers = [str(col or "").strip() for col in table[0]]
                        frames.append(pd.DataFrame(table[1:], columns=headers))
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        for num_col in ("settled_amount", "gateway_fee"):
            if num_col in df.columns:
                df[num_col] = pd.to_numeric(df[num_col], errors="coerce").fillna(0.0)
        return df

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def fetch(self, biz_date: str) -> CollectResult:
        conn_cfg = self.cfg.connectors.mock_finance
        base_url = conn_cfg.base_url.rstrip("/")
        username, password = conn_cfg.resolve_credentials()
        phase = "财务结算 SPA"

        download_dir = self.cfg.storage.downloads / biz_date
        download_dir.mkdir(parents=True, exist_ok=True)
        target_pdf = download_dir / f"finance_settlement_{biz_date}.pdf"

        api_items: list[dict] = []
        with self.browser_mgr.open_context(self.site_key) as (context, page):
            with self.browser_mgr.capture_on_error(page, "finance_collect"):
                settlements_url = f"{base_url}/finance/settlements?date={biz_date}"
                logger.info("Navigating to Finance SPA portal: %s", settlements_url)
                self.browser_mgr.step_goto(page, settlements_url, phase, f"打开财务结算后台 ({biz_date})")

                if "/finance/login" in page.url:
                    logger.info("Finance session missing or expired; signing in as %s", username)
                    self.browser_mgr.step_fill(page, 'input[name="username"]', username, phase, f"输入财务账号 {username}")
                    self.browser_mgr.step_fill(page, 'input[name="password"]', password, phase, "输入财务登录密码", mask=True)
                    self.browser_mgr.step_click(page, "#finance-login-btn", phase, "点击登录财务结算后台")
                    page.wait_for_load_state("domcontentloaded")
                    if "/finance/login" in page.url:
                        raise RuntimeError("Finance login failed: invalid credentials or login redirect")
                    self.browser_mgr.save_state(context, self.site_key)

                with page.expect_response(
                    lambda resp: "/api/finance/settlements" in resp.url and resp.status == 200
                ) as resp_info:
                    page.goto(settlements_url, wait_until="domcontentloaded")
                api_payload = resp_info.value.json()
                api_items = api_payload.get("items", [])
                page.wait_for_selector("#finance-tbody tr")
                self.browser_mgr.step_snap(
                    page,
                    phase,
                    "INTERCEPT",
                    "/api/finance/settlements",
                    f"拦截 XHR 结算接口（{len(api_items)} 条记录）",
                )

                self.browser_mgr.step_click(page, "#start-export-btn", phase, "点击生成 PDF 对账单任务")
                page.wait_for_selector("#download-pdf-btn", state="visible")
                self.browser_mgr.step_snap(page, phase, "WAIT", "#download-pdf-btn", "异步生成完成，PDF 下载就绪")

                with page.expect_download() as download_info:
                    self.browser_mgr.step_click(page, "#download-pdf-btn", phase, "下载 PDF 财务对账单")
                download = download_info.value
                download.save_as(str(target_pdf))

        pdf_df = self._parse_settlement_pdf(target_pdf)
        api_df = pd.DataFrame(api_items)
        final_df = pdf_df if not pdf_df.empty else api_df
        self.browser_mgr.emit(
            phase,
            "PARSE_PDF",
            target_pdf.name,
            f"pdfplumber 解析完成：{len(pdf_df)} 条结算流水",
        )
        logger.info(
            "Finance collected %d PDF rows (XHR intercepted %d items) -> %s",
            len(pdf_df),
            len(api_df),
            target_pdf.name,
        )
        return CollectResult(
            source_name=self.name,
            biz_date=biz_date,
            dataframe=final_df,
            raw_file_path=target_pdf,
            row_count=len(final_df),
        )
