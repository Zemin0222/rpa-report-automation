from __future__ import annotations

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from connectors.base import BaseConnector, CollectResult
from core.browser import BrowserManager
from core.config_loader import AppConfig
from core.logger import get_logger

logger = get_logger("rpa.connector.template")


class RealSiteTemplateConnector(BaseConnector):
    """通用真实业务系统采集器模板（复制此文件并按 TODO 修改即可快速接入新后台）。

    推荐接入步骤：
    1. 先在本地运行 `python main.py auth-login --site my_erp --url https://erp.example.com/login`
       弹出有头浏览器，人工完成扫码/滑块/二次验证后按 Enter 保存 `storage/auth_states/my_erp.json`。
    2. 如需录制页面点击路径，可运行：`playwright codegen --load-storage storage/auth_states/my_erp.json https://erp.example.com`
    3. 根据目标后台类型选择以下两种采集模式之一：
       - 模式 A（推荐）：`page.expect_response` 拦截 SPA 后端接口 JSON；
       - 模式 B：点击导出按钮并用 `page.expect_download()` 下载 CSV/Excel。
    4. 将采集结果标准化为统一的 pandas.DataFrame 后返回 `CollectResult`。
    """

    name = "real_site_template"
    site_key = "my_erp"  # TODO: 对应 storage/auth_states/{site_key}.json

    def __init__(self, cfg: AppConfig, browser_mgr: BrowserManager) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        reraise=True,
    )
    def fetch(self, biz_date: str) -> CollectResult:
        # TODO: 替换为目标系统业务列表页 URL
        target_url = f"https://erp.example.com/reports/daily?date={biz_date}"
        download_dir = self.cfg.storage.downloads / biz_date
        download_dir.mkdir(parents=True, exist_ok=True)
        out_csv = download_dir / f"{self.site_key}_{biz_date}.csv"

        with self.browser_mgr.open_context(self.site_key) as (context, page):
            with self.browser_mgr.capture_on_error(page, f"{self.site_key}_collect"):
                # 模式 A 示例：拦截前后端分离接口 JSON（若使用导出按钮，可删除此段改为模式 B）
                # with page.expect_response(lambda r: "/api/reports/list" in r.url and r.status == 200) as resp_info:
                #     page.goto(target_url, wait_until="domcontentloaded")
                # raw_items = resp_info.value.json().get("data", [])

                page.goto(target_url, wait_until="domcontentloaded")

                # TODO: 检查是否因 Cookie 过期跳转到登录页
                if "login" in page.url.lower():
                    raise RuntimeError(
                        f"[{self.site_key}] 登录态已过期，请先在本地运行 auth-login 更新 auth_states/{self.site_key}.json"
                    )

                # 模式 B 示例：点击页面导出按钮等待文件下载
                # page.wait_for_selector("#export-btn")
                # with page.expect_download() as dl_info:
                #     page.click("#export-btn")
                # dl_info.value.save_as(str(out_csv))

                # 更新保存最新 Cookie 状态
                self.browser_mgr.save_state(context, self.site_key)

        # TODO: 读取下载文件或接口 JSON 并统一字段名（如 channel, order_amount 等）
        df = pd.read_csv(out_csv, encoding="utf-8-sig") if out_csv.exists() else pd.DataFrame()
        return CollectResult(
            source_name=self.name,
            biz_date=biz_date,
            dataframe=df,
            raw_file_path=out_csv if out_csv.exists() else None,
            row_count=len(df),
        )
