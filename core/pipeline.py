from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import pandas as pd

from connectors.mock_oa_submitter import MockOaSubmitter
from connectors.registry import apply_column_mapping, build_connector
from core.browser import BrowserManager, BrowserStepError
from core.config_loader import AppConfig, SourceSpec, load_config
from core.housekeeping import cleanup_storage
from core.live_observer import LiveRunTracker
from core.logger import get_logger
from notifiers.email_sender import send_email_report
from notifiers.webhook import send_failure_alert, send_webhook_notification
from processors.etl import run_etl
from processors.summary_builder import build_summary
from reporters.excel_reporter import generate_excel_report
from reporters.word_reporter import generate_word_report
from storage.state_db import StateDB

logger = get_logger("rpa.pipeline")

_BROWSER_CONNECTORS = {"mock_crm", "mock_ads", "mock_finance", "generic_web", "real_site_template"}


@dataclass
class PipelineOutcome:
    run_id: int
    biz_date: str
    status: str  # SUCCESS | SKIPPED | LOCKED | FAILED
    duration_sec: float = 0.0
    row_count: int = 0
    anomaly_count: int = 0
    reconcile_diff_count: int = 0
    oa_ticket_id: str | None = None
    excel_path: Path | None = None
    word_path: Path | None = None
    notify_preview_path: Path | None = None
    screenshot_path: Path | None = None
    summary_text: str = ""
    error_message: str = ""
    kpis: dict[str, Any] | None = None


def _run_failure_drill(cfg: AppConfig, browser_mgr: BrowserManager, target_date: str) -> None:
    """Deliberately wait for a missing element to showcase auto-screenshot & failure audit."""
    base_url = cfg.connectors.mock_crm.base_url.rstrip("/")
    username, password = cfg.connectors.mock_crm.resolve_credentials()
    with browser_mgr.open_context("mock_crm") as (context, page):
        with browser_mgr.capture_on_error(page, "drill_crm_redesign"):
            browser_mgr.step_goto(page, f"{base_url}/crm/orders?date={target_date}", "故障演练", "打开 CRM 订单页面")
            if "/crm/login" in page.url:
                browser_mgr.step_fill(page, 'input[name="username"]', username, "故障演练", f"输入账号 {username}")
                browser_mgr.step_fill(page, 'input[name="password"]', password, "故障演练", "输入密码", mask=True)
                browser_mgr.step_click(page, "#crm-login-btn", "故障演练", "点击登录")
                page.wait_for_load_state("domcontentloaded")
                browser_mgr.save_state(context, "mock_crm")
                browser_mgr.step_goto(page, f"{base_url}/crm/orders?date={target_date}", "故障演练", "进入 CRM 订单页")
            page.wait_for_selector("#redesigned-export-v2-btn", timeout=1200)


def _is_source_enabled(cfg: AppConfig, source: SourceSpec) -> bool:
    if not source.enabled:
        return False
    legacy_conn = getattr(cfg.connectors, source.connector, None)
    if legacy_conn is not None and hasattr(legacy_conn, "enabled"):
        return bool(legacy_conn.enabled)
    return True


def _needs_browser(source: SourceSpec) -> bool:
    return source.connector in _BROWSER_CONNECTORS or ":" in source.connector


def _merge_role_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    non_empty = [df for df in frames if df is not None and not df.empty]
    if not non_empty:
        return pd.DataFrame()
    if len(non_empty) == 1:
        return non_empty[0]
    return pd.concat(non_empty, ignore_index=True)


def run_daily_pipeline(
    biz_date: str | None = None,
    force: bool = False,
    dry_run: bool | None = None,
    cfg: AppConfig | None = None,
    simulate_failure: bool = False,
    job_name: str | None = None,
    observer: LiveRunTracker | None = None,
    session_id: str | None = None,
    visual_delay_ms: int = 0,
    fresh_login: bool = False,
    headless_override: bool | None = None,
) -> PipelineOutcome:
    """Execute the full cross-system collection, ETL, reconciliation, reporting, OA submit, and delivery."""
    cfg = cfg or load_config()
    job_def = cfg.get_job(job_name)
    active_job_name = job_def.job_name
    target_date = biz_date or date.today().isoformat()
    db = StateDB(cfg.storage.db_path)

    def _finish_live(outcome: PipelineOutcome) -> PipelineOutcome:
        if observer and session_id:
            observer.finish_session(
                session_id,
                outcome.status,
                {
                    "run_id": outcome.run_id,
                    "biz_date": outcome.biz_date,
                    "status": outcome.status,
                    "duration_sec": outcome.duration_sec,
                    "row_count": outcome.row_count,
                    "anomaly_count": outcome.anomaly_count,
                    "reconcile_diff_count": outcome.reconcile_diff_count,
                    "oa_ticket_id": outcome.oa_ticket_id,
                    "error_message": outcome.error_message,
                },
            )
        return outcome

    active_lock = db.get_running_lock(active_job_name, target_date)
    if active_lock is not None:
        logger.warning(
            "Concurrent run blocked by active RUNNING lock: job=%s date=%s run_id=%s",
            active_job_name,
            target_date,
            active_lock["id"],
        )
        return _finish_live(
            PipelineOutcome(
                run_id=int(active_lock["id"]),
                biz_date=target_date,
                status="LOCKED",
                error_message=f"Job '{active_job_name}' for {target_date} is already RUNNING (run_id={active_lock['id']})",
            )
        )

    if not force and not simulate_failure:
        existing = db.get_successful_run(active_job_name, target_date)
        if existing:
            logger.info(
                "Idempotency hit for %s (%s); skipping run_id=%s",
                active_job_name,
                target_date,
                existing["id"],
            )
            return _finish_live(
                PipelineOutcome(
                    run_id=int(existing["id"]),
                    biz_date=target_date,
                    status="SKIPPED",
                    duration_sec=float(existing.get("duration_sec") or 0.0),
                    row_count=int(existing.get("row_count") or 0),
                    anomaly_count=int(existing.get("anomaly_count") or 0),
                    excel_path=Path(existing["excel_path"]) if existing.get("excel_path") else None,
                    word_path=Path(existing["word_path"]) if existing.get("word_path") else None,
                    notify_preview_path=(
                        Path(existing["notify_preview_path"])
                        if existing.get("notify_preview_path")
                        else None
                    ),
                    summary_text=existing.get("summary_text") or "",
                )
            )

    run_id = db.start_run(active_job_name, target_date)
    start_ts = time.perf_counter()
    use_headless = cfg.browser.headless if headless_override is None else headless_override
    use_slow_mo = cfg.browser.slow_mo_ms
    if not use_headless and use_slow_mo == 0 and visual_delay_ms > 0:
        use_slow_mo = 200

    try:
        role_frames: dict[str, list[pd.DataFrame]] = {
            "orders": [],
            "ads": [],
            "finance": [],
            "extra_cost": [],
        }
        enabled_sources = [s for s in job_def.sources if _is_source_enabled(cfg, s)]
        browser_sources = [s for s in enabled_sources if _needs_browser(s)]
        non_browser_sources = [s for s in enabled_sources if not _needs_browser(s)]
        oa_enabled = bool(cfg.connectors.mock_oa.enabled)
        need_browser_mgr = bool(simulate_failure or browser_sources or oa_enabled)

        oa_ticket_id: str | None = None

        if need_browser_mgr:
            with BrowserManager(
                auth_dir=cfg.storage.auth_states,
                screenshot_dir=cfg.storage.screenshots,
                download_dir=cfg.storage.downloads,
                headless=use_headless,
                timeout_ms=cfg.browser.timeout_ms,
                slow_mo_ms=use_slow_mo,
                observer=observer,
                session_id=session_id,
                visual_delay_ms=visual_delay_ms,
                fresh_login=fresh_login,
            ) as browser_mgr:
                if simulate_failure:
                    db.log_step(run_id, "simulate_failure_drill", "STARTED")
                    _run_failure_drill(cfg, browser_mgr, target_date)

                for source in browser_sources:
                    t0 = time.perf_counter()
                    connector = build_connector(source, cfg, browser_mgr=browser_mgr, db=db)
                    res = connector.fetch(target_date)
                    df_part = apply_column_mapping(res.dataframe, source.column_mapping)
                    role_frames[source.role].append(df_part)
                    db.log_step(
                        run_id,
                        f"collect_{source.id}",
                        "DONE",
                        f"connector={source.connector}, rows={res.row_count}",
                        duration_ms=(time.perf_counter() - t0) * 1000,
                    )

                for source in non_browser_sources:
                    t0 = time.perf_counter()
                    connector = build_connector(source, cfg, browser_mgr=None, db=db)
                    res = connector.fetch(target_date)
                    df_part = apply_column_mapping(res.dataframe, source.column_mapping)
                    role_frames[source.role].append(df_part)
                    db.log_step(
                        run_id,
                        f"collect_{source.id}",
                        "DONE",
                        f"connector={source.connector}, rows={res.row_count}",
                        duration_ms=(time.perf_counter() - t0) * 1000,
                    )

                orders_df = _merge_role_frames(role_frames["orders"])
                ads_df = _merge_role_frames(role_frames["ads"])
                finance_df = _merge_role_frames(role_frames["finance"])
                drop_df = _merge_role_frames(role_frames["extra_cost"])

                t0 = time.perf_counter()
                prev_sales = db.get_previous_kpi_map(active_job_name, target_date)
                etl = run_etl(
                    biz_date=target_date,
                    orders_df=orders_df,
                    ads_df=ads_df,
                    drop_df=drop_df,
                    thresholds=cfg.thresholds,
                    prev_channel_sales=prev_sales,
                    finance_df=finance_df,
                )
                summary_text = build_summary(etl, cfg.llm)
                reconcile_diff_count = int(etl.reconcile_summary.get("discrepancy_count", 0))
                if observer and session_id:
                    observer.record_event(
                        session_id,
                        "ETL 与三方对账",
                        "COMPUTE",
                        "run_etl",
                        f"完成 {len(etl.channel_df)} 个渠道汇总，发现 {len(etl.anomalies)} 个异常渠道、{reconcile_diff_count} 笔对账差异",
                    )
                db.log_step(
                    run_id,
                    "etl_and_reconcile",
                    "DONE",
                    f"channels={len(etl.channel_df)}, anomalies={len(etl.anomalies)}, diffs={reconcile_diff_count}",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

                t0 = time.perf_counter()
                trend_rows = db.get_daily_trend(active_job_name, limit_days=14)
                excel_path = generate_excel_report(etl, summary_text, cfg.storage.outputs, trend_rows=trend_rows)
                word_path = generate_word_report(etl, summary_text, cfg.storage.outputs, trend_rows=trend_rows)
                if observer and session_id:
                    observer.record_event(
                        session_id,
                        "报表自动生成",
                        "RENDER",
                        excel_path.name,
                        f"已生成图表版日报 {excel_path.name} 与周报 {word_path.name}",
                    )
                db.log_step(
                    run_id,
                    "render_reports",
                    "DONE",
                    f"{excel_path.name}, {word_path.name}",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

                if oa_enabled:
                    t0 = time.perf_counter()
                    oa_ticket_id = MockOaSubmitter(cfg, browser_mgr).submit(
                        target_date, etl, summary_text, excel_path
                    )
                    db.log_step(
                        run_id,
                        "submit_oa_report",
                        "DONE",
                        f"ticket={oa_ticket_id}",
                        duration_ms=(time.perf_counter() - t0) * 1000,
                    )
        else:
            for source in non_browser_sources:
                t0 = time.perf_counter()
                connector = build_connector(source, cfg, browser_mgr=None, db=db)
                res = connector.fetch(target_date)
                df_part = apply_column_mapping(res.dataframe, source.column_mapping)
                role_frames[source.role].append(df_part)
                db.log_step(
                    run_id,
                    f"collect_{source.id}",
                    "DONE",
                    f"connector={source.connector}, rows={res.row_count}",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

            orders_df = _merge_role_frames(role_frames["orders"])
            ads_df = _merge_role_frames(role_frames["ads"])
            finance_df = _merge_role_frames(role_frames["finance"])
            drop_df = _merge_role_frames(role_frames["extra_cost"])

            t0 = time.perf_counter()
            prev_sales = db.get_previous_kpi_map(active_job_name, target_date)
            etl = run_etl(
                biz_date=target_date,
                orders_df=orders_df,
                ads_df=ads_df,
                drop_df=drop_df,
                thresholds=cfg.thresholds,
                prev_channel_sales=prev_sales,
                finance_df=finance_df,
            )
            summary_text = build_summary(etl, cfg.llm)
            reconcile_diff_count = int(etl.reconcile_summary.get("discrepancy_count", 0))
            db.log_step(
                run_id,
                "etl_and_reconcile",
                "DONE",
                f"channels={len(etl.channel_df)}, anomalies={len(etl.anomalies)}, diffs={reconcile_diff_count}",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

            t0 = time.perf_counter()
            trend_rows = db.get_daily_trend(active_job_name, limit_days=14)
            excel_path = generate_excel_report(etl, summary_text, cfg.storage.outputs, trend_rows=trend_rows)
            word_path = generate_word_report(etl, summary_text, cfg.storage.outputs, trend_rows=trend_rows)
            db.log_step(
                run_id,
                "render_reports",
                "DONE",
                f"{excel_path.name}, {word_path.name}",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        t0 = time.perf_counter()
        notify_preview = send_webhook_notification(
            cfg.notifier,
            etl,
            summary_text,
            excel_path,
            cfg.storage.outputs,
            force_dry_run=dry_run,
        )
        send_email_report(
            cfg.notifier,
            target_date,
            summary_text,
            [excel_path, word_path],
            force_dry_run=dry_run,
        )
        if observer and session_id:
            observer.record_event(
                session_id,
                "通知分发",
                "NOTIFY",
                notify_preview.name,
                f"已生成群通知卡片 {notify_preview.name}",
            )
        db.log_step(
            run_id,
            "notify",
            "DONE",
            notify_preview.name,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

        elapsed = time.perf_counter() - start_ts
        total_rows = len(orders_df) + len(ads_df) + len(finance_df) + len(drop_df)
        db.finish_success(
            run_id=run_id,
            duration_sec=elapsed,
            row_count=total_rows,
            anomaly_count=len(etl.anomalies),
            excel_path=str(excel_path),
            word_path=str(word_path),
            notify_preview_path=str(notify_preview),
            summary_text=summary_text,
            kpi_payload=etl.kpis,
        )

        logger.info("Pipeline completed in %.2fs (run_id=%d)", elapsed, run_id)
        try:
            assert cfg.storage.backups is not None
            db.backup_database(cfg.storage.backups, keep_count=7)
        except Exception as bkp_exc:
            logger.warning("Post-run DB backup failed (ignored): %s", bkp_exc)

        try:
            cleanup_storage(cfg.storage, cfg.retention, dry_run=False)
        except Exception as clean_exc:
            logger.warning("Post-run housekeeping failed (ignored): %s", clean_exc)

        return _finish_live(
            PipelineOutcome(
                run_id=run_id,
                biz_date=target_date,
                status="SUCCESS",
                duration_sec=round(elapsed, 2),
                row_count=total_rows,
                anomaly_count=len(etl.anomalies),
                reconcile_diff_count=reconcile_diff_count,
                oa_ticket_id=oa_ticket_id,
                excel_path=excel_path,
                word_path=word_path,
                notify_preview_path=notify_preview,
                summary_text=summary_text,
                kpis=etl.kpis,
            )
        )

    except Exception as exc:
        elapsed = time.perf_counter() - start_ts
        shot_path: Path | None = getattr(exc, "screenshot_path", None)
        if shot_path is None and isinstance(exc.__cause__, BrowserStepError):
            shot_path = exc.__cause__.screenshot_path

        db.log_step(
            run_id,
            "error",
            "FAILED",
            str(exc),
            duration_ms=elapsed * 1000,
        )
        db.finish_failed(
            run_id=run_id,
            duration_sec=elapsed,
            error_message=str(exc),
            screenshot_path=str(shot_path) if shot_path else None,
        )
        try:
            send_failure_alert(
                cfg.notifier,
                job_name=active_job_name,
                biz_date=target_date,
                error_message=str(exc),
                screenshot_path=shot_path,
                output_dir=cfg.storage.outputs,
                force_dry_run=dry_run,
            )
        except Exception as alert_exc:
            logger.warning("Failure alert dispatch failed (ignored): %s", alert_exc)

        logger.error("Pipeline failed in %.2fs: %s", elapsed, exc)
        return _finish_live(
            PipelineOutcome(
                run_id=run_id,
                biz_date=target_date,
                status="FAILED",
                duration_sec=round(elapsed, 2),
                screenshot_path=shot_path,
                error_message=str(exc),
            )
        )


def run_backfill_pipeline(
    days: int = 7,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    dry_run: bool | None = True,
    cfg: AppConfig | None = None,
    job_name: str | None = None,
) -> list[PipelineOutcome]:
    """Chronologically backfill N days (or date range) so DoD and trend series populate cleanly."""
    cfg = cfg or load_config()
    if start_date and end_date:
        d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        d_end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        d_end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else date.today()
        d_start = d_end - timedelta(days=max(1, days) - 1)

    outcomes: list[PipelineOutcome] = []
    curr = d_start
    while curr <= d_end:
        outcomes.append(
            run_daily_pipeline(
                biz_date=curr.isoformat(),
                force=force,
                dry_run=dry_run,
                cfg=cfg,
                job_name=job_name,
            )
        )
        curr += timedelta(days=1)
    return outcomes
