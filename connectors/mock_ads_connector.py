from __future__ import annotations

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from connectors.base import BaseConnector, CollectResult
from core.browser import BrowserManager
from core.config_loader import AppConfig
from core.logger import get_logger

logger = get_logger("rpa.connector.ads")


class MockAdsConnector(BaseConnector):
    """Logs into the Ads portal via Playwright and scrapes paginated campaign tables."""

    name = "mock_ads"
    site_key = "mock_ads"

    def __init__(self, cfg: AppConfig, browser_mgr: BrowserManager) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def fetch(self, biz_date: str) -> CollectResult:
        conn_cfg = self.cfg.connectors.mock_ads
        base_url = conn_cfg.base_url.rstrip("/")
        username, password = conn_cfg.resolve_credentials()
        phase = "广告投放后台"

        scraped_rows: list[dict] = []
        with self.browser_mgr.open_context(self.site_key) as (context, page):
            with self.browser_mgr.capture_on_error(page, "ads_collect"):
                ads_url = f"{base_url}/ads/campaigns?date={biz_date}&page=1"
                logger.info("Navigating to Ads campaigns page: %s", ads_url)
                self.browser_mgr.step_goto(page, ads_url, phase, f"打开广告投放后台 ({biz_date})")

                if "/ads/login" in page.url:
                    logger.info("Ads session missing or expired; signing in as %s", username)
                    self.browser_mgr.step_fill(page, 'input[name="username"]', username, phase, f"输入广告后台账号 {username}")
                    self.browser_mgr.step_fill(page, 'input[name="password"]', password, phase, "输入广告后台密码", mask=True)
                    self.browser_mgr.step_click(page, "#ads-login-btn", phase, "点击登录广告投放后台")
                    page.wait_for_load_state("domcontentloaded")
                    if "/ads/login" in page.url:
                        raise RuntimeError("Ads login failed: invalid credentials or login redirect")
                    self.browser_mgr.save_state(context, self.site_key)
                    self.browser_mgr.step_goto(page, ads_url, phase, "进入第 1 页投放计划明细")

                page_num = 1
                while True:
                    page.wait_for_selector("#ads-table tbody tr")
                    rows = page.locator("#ads-table tbody tr")
                    count = rows.count()
                    for idx in range(count):
                        cells = rows.nth(idx).locator("td").all_inner_texts()
                        if len(cells) >= 7:
                            scraped_rows.append(
                                {
                                    "campaign_id": cells[0].strip(),
                                    "biz_date": cells[1].strip(),
                                    "channel": cells[2].strip(),
                                    "campaign_name": cells[3].strip(),
                                    "impressions": int(cells[4].replace(",", "").strip()),
                                    "clicks": int(cells[5].replace(",", "").strip()),
                                    "ad_spend": float(cells[6].replace(",", "").strip()),
                                }
                            )
                    self.browser_mgr.step_snap(
                        page,
                        phase,
                        "SCRAPE",
                        "#ads-table",
                        f"抓取第 {page_num} 页广告消耗数据（{count} 条）",
                    )

                    next_btn = page.locator("#next-page")
                    if next_btn.count() == 0:
                        break
                    self.browser_mgr.step_click(page, "#next-page", phase, f"点击翻页至第 {page_num + 1} 页")
                    page.wait_for_load_state("domcontentloaded")
                    page_num += 1

        df = pd.DataFrame(scraped_rows)
        raw_out = self.cfg.storage.downloads / biz_date / f"ads_campaigns_{biz_date}.csv"
        raw_out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(raw_out, index=False, encoding="utf-8-sig")

        logger.info("Ads scraper collected %d rows -> %s", len(df), raw_out.name)
        return CollectResult(
            source_name=self.name,
            biz_date=biz_date,
            dataframe=df,
            raw_file_path=raw_out,
            row_count=len(df),
        )
