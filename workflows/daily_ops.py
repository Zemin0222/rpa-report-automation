from __future__ import annotations

import time
from typing import Any

import pandas as pd

from ai.gateway import AiGateway
from connectors.mock_oa_submitter import MockOaSubmitter
from connectors.registry import apply_column_mapping, build_connector
from core.config_loader import AppConfig, JobDefinition, SourceSpec
from notifiers.email_sender import send_email_report
from notifiers.webhook import send_webhook_notification
from processors.etl import EtlResult, run_etl
from processors.summary_builder import build_summary
from reporters.excel_reporter import generate_excel_report
from reporters.word_reporter import generate_word_report
from workflows.base import (
    CollectedData,
    DeliveryResult,
    ProcessResult,
    RunContext,
)

_BROWSER_CONNECTORS = {
    "mock_crm",
    "mock_ads",
    "mock_finance",
    "generic_web",
    "real_site_template",
}


def _is_source_enabled(cfg: AppConfig, source: SourceSpec) -> bool:
    if not source.enabled:
        return False
    legacy_conn = getattr(cfg.connectors, source.connector, None)
    if legacy_conn is not None and hasattr(legacy_conn, "enabled"):
        return bool(legacy_conn.enabled)
    return True


def _source_needs_browser(source: SourceSpec) -> bool:
    return source.connector in _BROWSER_CONNECTORS or ":" in source.connector


def _merge_role_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [df for df in frames if df is not None and not df.empty]
    if not non_empty:
        return pd.DataFrame()
    if len(non_empty) == 1:
        return non_empty[0]
    return pd.concat(non_empty, ignore_index=True)


class DailyOpsWorkflow:
    """Default cross-system scenario: CRM + Ads + Finance PDF -> ETL -> Reports + OA -> Notify."""

    key = "daily_ops_report"
    title = "每日全渠道对账与 OA 日报"
    description = "自动采集 CRM/广告/财务结算单，完成三方对账、生成图文报表并回填提交 OA 日报"
    view_type = "daily_ops"

    def needs_browser(
        self,
        cfg: AppConfig,
        job_def: JobDefinition,
        simulate_failure: bool = False,
    ) -> bool:
        enabled_sources = [s for s in job_def.sources if _is_source_enabled(cfg, s)]
        has_browser_source = any(_source_needs_browser(s) for s in enabled_sources)
        oa_enabled = bool(cfg.connectors.mock_oa.enabled)
        return bool(simulate_failure or has_browser_source or oa_enabled)

    def run_failure_drill(self, ctx: RunContext) -> None:
        """Deliberately wait for a missing selector to verify screenshot + failure alert."""
        if ctx.browser_mgr is None:
            raise RuntimeError("Failure drill requires an active BrowserManager")
        base_url = ctx.cfg.connectors.mock_crm.base_url.rstrip("/")
        username, password = ctx.cfg.connectors.mock_crm.resolve_credentials()
        with ctx.browser_mgr.open_context("mock_crm") as (context, page):
            with ctx.browser_mgr.capture_on_error(page, "drill_crm_redesign"):
                ctx.browser_mgr.step_goto(
                    page,
                    f"{base_url}/crm/orders?date={ctx.biz_date}",
                    "故障演练",
                    "打开 CRM 订单页面",
                )
                if "/crm/login" in page.url:
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
                    ctx.browser_mgr.step_click(page, "#crm-login-btn", "故障演练", "点击登录")
                    page.wait_for_load_state("domcontentloaded")
                    ctx.browser_mgr.save_state(context, "mock_crm")
                    ctx.browser_mgr.step_goto(
                        page,
                        f"{base_url}/crm/orders?date={ctx.biz_date}",
                        "故障演练",
                        "进入 CRM 订单页",
                    )
                page.wait_for_selector("#redesigned-export-v2-btn", timeout=1200)

    def collect(self, ctx: RunContext) -> CollectedData:
        role_frames: dict[str, list[pd.DataFrame]] = {
            "orders": [],
            "ads": [],
            "finance": [],
            "extra_cost": [],
        }
        enabled_sources = [s for s in ctx.job_def.sources if _is_source_enabled(ctx.cfg, s)]
        browser_sources = [s for s in enabled_sources if _source_needs_browser(s)]
        non_browser_sources = [s for s in enabled_sources if not _source_needs_browser(s)]

        for source in browser_sources:
            t0 = time.perf_counter()
            connector = build_connector(
                source,
                ctx.cfg,
                browser_mgr=ctx.browser_mgr,
                db=ctx.db,
            )
            res = connector.fetch(ctx.biz_date)
            df_part = apply_column_mapping(res.dataframe, source.column_mapping)
            role_frames[source.role].append(df_part)
            ctx.log_step(
                f"collect_{source.id}",
                "DONE",
                f"connector={source.connector}, rows={res.row_count}",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        for source in non_browser_sources:
            t0 = time.perf_counter()
            connector = build_connector(source, ctx.cfg, browser_mgr=None, db=ctx.db)
            res = connector.fetch(ctx.biz_date)
            df_part = apply_column_mapping(res.dataframe, source.column_mapping)
            role_frames[source.role].append(df_part)
            ctx.log_step(
                f"collect_{source.id}",
                "DONE",
                f"connector={source.connector}, rows={res.row_count}",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        total_rows = sum(
            len(df) for frames in role_frames.values() for df in frames if df is not None
        )
        return CollectedData(frames_by_role=role_frames, row_count=total_rows)

    def process(self, ctx: RunContext, data: CollectedData) -> ProcessResult:
        t0 = time.perf_counter()
        orders_df = _merge_role_frames(data.frames_by_role.get("orders", []))
        ads_df = _merge_role_frames(data.frames_by_role.get("ads", []))
        finance_df = _merge_role_frames(data.frames_by_role.get("finance", []))
        drop_df = _merge_role_frames(data.frames_by_role.get("extra_cost", []))

        prev_sales = ctx.db.get_previous_kpi_map(ctx.job_def.job_name, ctx.biz_date)
        etl = run_etl(
            biz_date=ctx.biz_date,
            orders_df=orders_df,
            ads_df=ads_df,
            drop_df=drop_df,
            thresholds=ctx.cfg.thresholds,
            prev_channel_sales=prev_sales,
            finance_df=finance_df,
        )
        gateway = AiGateway(ctx.cfg.llm, db=ctx.db)
        trend_rows = ctx.db.get_daily_trend(ctx.job_def.job_name, limit_days=14)
        summary_text = build_summary(etl, ctx.cfg.llm, gateway=gateway)

        for ch in etl.kpis.get("channels", []):
            if ch.get("is_anomaly"):
                diag = gateway.explain_anomaly(ch, trend_context=trend_rows)
                ch["ai_diagnosis"] = {
                    **diag.data.model_dump(),
                    "source": diag.source,
                    "model": diag.model,
                    "latency_ms": diag.latency_ms,
                }

        for rec in etl.kpis.get("reconcile_items", []):
            attr = gateway.attribute_reconcile_diff(rec)
            rec["ai_attribution"] = {
                **attr.data.model_dump(),
                "source": attr.source,
                "model": attr.model,
                "latency_ms": attr.latency_ms,
            }

        etl.kpis["ai_stats"] = ctx.db.get_ai_daily_stats()
        reconcile_diff_count = int(etl.reconcile_summary.get("discrepancy_count", 0))
        total_rows = len(orders_df) + len(ads_df) + len(finance_df) + len(drop_df)

        ctx.record_live(
            "ETL 与三方对账",
            "COMPUTE",
            "run_etl",
            (
                f"完成 {len(etl.channel_df)} 个渠道汇总，发现 {len(etl.anomalies)} 个异常渠道、"
                f"{reconcile_diff_count} 笔对账差异"
            ),
        )
        ctx.log_step(
            "etl_and_reconcile",
            "DONE",
            (
                f"channels={len(etl.channel_df)}, anomalies={len(etl.anomalies)}, "
                f"diffs={reconcile_diff_count}"
            ),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

        return ProcessResult(
            row_count=total_rows,
            anomaly_count=len(etl.anomalies),
            reconcile_diff_count=reconcile_diff_count,
            summary_text=summary_text,
            kpis=etl.kpis,
            payload=etl,
        )

    def deliver(self, ctx: RunContext, result: ProcessResult) -> DeliveryResult:
        etl: EtlResult = result.payload
        t0 = time.perf_counter()
        trend_rows = ctx.db.get_daily_trend(ctx.job_def.job_name, limit_days=14)
        excel_path = generate_excel_report(
            etl,
            result.summary_text,
            ctx.cfg.storage.outputs,
            trend_rows=trend_rows,
        )
        word_path = generate_word_report(
            etl,
            result.summary_text,
            ctx.cfg.storage.outputs,
            trend_rows=trend_rows,
        )
        ctx.record_live(
            "报表自动生成",
            "RENDER",
            excel_path.name,
            f"已生成图表版日报 {excel_path.name} 与周报 {word_path.name}",
        )
        ctx.log_step(
            "render_reports",
            "DONE",
            f"{excel_path.name}, {word_path.name}",
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

        oa_ticket_id: str | None = None
        if ctx.cfg.connectors.mock_oa.enabled and ctx.browser_mgr is not None:
            t_oa = time.perf_counter()
            oa_ticket_id = MockOaSubmitter(ctx.cfg, ctx.browser_mgr).submit(
                ctx.biz_date,
                etl,
                result.summary_text,
                excel_path,
            )
            ctx.log_step(
                "submit_oa_report",
                "DONE",
                f"ticket={oa_ticket_id}",
                duration_ms=(time.perf_counter() - t_oa) * 1000,
            )

        t_notify = time.perf_counter()
        notify_preview = send_webhook_notification(
            ctx.cfg.notifier,
            etl,
            result.summary_text,
            excel_path,
            ctx.cfg.storage.outputs,
            force_dry_run=ctx.dry_run,
        )
        send_email_report(
            ctx.cfg.notifier,
            ctx.biz_date,
            result.summary_text,
            [excel_path, word_path],
            force_dry_run=ctx.dry_run,
        )
        ctx.record_live(
            "通知分发",
            "NOTIFY",
            notify_preview.name,
            f"已生成群通知卡片 {notify_preview.name}",
        )
        ctx.log_step(
            "notify",
            "DONE",
            notify_preview.name,
            duration_ms=(time.perf_counter() - t_notify) * 1000,
        )

        return DeliveryResult(
            oa_ticket_id=oa_ticket_id,
            excel_path=excel_path,
            word_path=word_path,
            notify_preview_path=notify_preview,
        )

    def kpi_view(self, result: ProcessResult) -> dict[str, Any]:
        return dict(result.kpis)
