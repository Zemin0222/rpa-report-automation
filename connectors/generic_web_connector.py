from __future__ import annotations

from pathlib import Path
from typing import Any
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from connectors.base import BaseConnector, CollectResult
from core.browser import BrowserManager
from core.config_loader import AppConfig, SourceSpec
from core.logger import get_logger

logger = get_logger("rpa.connector.generic_web")


class GenericWebConnector(BaseConnector):
    """Config-driven Web RPA connector supporting API JSON interception or file download."""

    name = "generic_web"

    def __init__(
        self,
        cfg: AppConfig,
        browser_mgr: BrowserManager,
        source_spec: SourceSpec | None = None,
    ) -> None:
        self.cfg = cfg
        self.browser_mgr = browser_mgr
        self.source_spec = source_spec

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def fetch(self, biz_date: str) -> CollectResult:
        params: dict[str, Any] = self.source_spec.params if self.source_spec else {}
        site_key = str(params.get("site_key") or (self.source_spec.id if self.source_spec else "generic_web"))
        target_tpl = str(params.get("target_url", "")).strip()
        if not target_tpl:
            raise ValueError(f"GenericWebConnector ({site_key}) requires params.target_url")

        target_url = target_tpl.format(biz_date=biz_date)
        mode = str(params.get("mode", "download")).strip().lower()
        api_pattern = str(params.get("api_pattern", "/api/")).strip()
        json_data_key = str(params.get("json_data_key", "items")).strip()
        export_selector = str(params.get("export_selector", "#export-btn")).strip()
        col_map = self.source_spec.column_mapping if self.source_spec else {}
        if isinstance(params.get("column_mapping"), dict):
            col_map = {**col_map, **params["column_mapping"]}

        download_dir = self.cfg.storage.downloads / biz_date
        download_dir.mkdir(parents=True, exist_ok=True)
        out_file: Path | None = None
        df = pd.DataFrame()

        with self.browser_mgr.open_context(site_key) as (context, page):
            with self.browser_mgr.capture_on_error(page, f"{site_key}_collect"):
                if mode == "api_intercept":
                    with page.expect_response(
                        lambda r: api_pattern in r.url and r.status == 200
                    ) as resp_info:
                        page.goto(target_url, wait_until="domcontentloaded")
                    if "login" in page.url.lower():
                        raise RuntimeError(
                            f"[{site_key}] 登录态已失效，请运行: python main.py auth-login --site {site_key}"
                        )
                    payload = resp_info.value.json()
                    records = payload.get(json_data_key, payload) if isinstance(payload, dict) else payload
                    df = pd.DataFrame(records if isinstance(records, list) else [])
                else:
                    page.goto(target_url, wait_until="domcontentloaded")
                    if "login" in page.url.lower():
                        raise RuntimeError(
                            f"[{site_key}] 登录态已失效，请运行: python main.py auth-login --site {site_key}"
                        )
                    page.wait_for_selector(export_selector)
                    with page.expect_download() as dl_info:
                        page.click(export_selector)
                    dl = dl_info.value
                    suffix = Path(dl.suggested_filename or "export.csv").suffix or ".csv"
                    out_file = download_dir / f"{site_key}_{biz_date}{suffix}"
                    dl.save_as(str(out_file))
                    if suffix.lower() in {".xlsx", ".xls"}:
                        df = pd.read_excel(out_file)
                    else:
                        df = pd.read_csv(out_file, encoding="utf-8-sig")

                self.browser_mgr.save_state(context, site_key)

        if col_map and not df.empty:
            df = df.rename(columns=col_map)

        return CollectResult(
            source_name=site_key,
            biz_date=biz_date,
            dataframe=df,
            raw_file_path=out_file,
            row_count=len(df),
        )
