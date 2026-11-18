from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core.browser import BrowserManager, BrowserStepError
from core.config_loader import AppConfig, load_config
from core.housekeeping import cleanup_storage
from core.live_observer import LiveRunTracker
from core.logger import get_logger
from notifiers.webhook import send_failure_alert
from storage.state_db import StateDB
from workflows.base import RunContext, Workflow

logger = get_logger("rpa.runner")


@dataclass
class PipelineOutcome:
    """Unified execution outcome returned across all workflow scenarios."""

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


def run_workflow(
    wf: Workflow,
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
    """Execute any Workflow plugin with unified locking, idempotency, live tracking, backup, and alerting."""
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
                error_message=(
                    f"Job '{active_job_name}' for {target_date} is already RUNNING "
                    f"(run_id={active_lock['id']})"
                ),
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

    ctx = RunContext(
        run_id=run_id,
        cfg=cfg,
        job_def=job_def,
        db=db,
        biz_date=target_date,
        dry_run=dry_run,
        simulate_failure=simulate_failure,
        observer=observer,
        session_id=session_id,
    )

    try:
        need_browser = wf.needs_browser(cfg, job_def, simulate_failure=simulate_failure)
        if need_browser:
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
                ctx.browser_mgr = browser_mgr
                if simulate_failure:
                    ctx.log_step("simulate_failure_drill", "STARTED")
                    wf.run_failure_drill(ctx)

                collected = wf.collect(ctx)
                processed = wf.process(ctx, collected)
                delivery = wf.deliver(ctx, processed)
        else:
            if simulate_failure:
                ctx.log_step("simulate_failure_drill", "STARTED")
                wf.run_failure_drill(ctx)

            collected = wf.collect(ctx)
            processed = wf.process(ctx, collected)
            delivery = wf.deliver(ctx, processed)

        kpi_payload = wf.kpi_view(processed)
        elapsed = time.perf_counter() - start_ts

        db.finish_success(
            run_id=run_id,
            duration_sec=elapsed,
            row_count=processed.row_count,
            anomaly_count=processed.anomaly_count,
            excel_path=str(delivery.excel_path) if delivery.excel_path else None,
            word_path=str(delivery.word_path) if delivery.word_path else None,
            notify_preview_path=str(delivery.notify_preview_path) if delivery.notify_preview_path else None,
            summary_text=processed.summary_text,
            kpi_payload=kpi_payload,
        )

        logger.info("Workflow '%s' completed in %.2fs (run_id=%d)", wf.key, elapsed, run_id)
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
                row_count=processed.row_count,
                anomaly_count=processed.anomaly_count,
                reconcile_diff_count=processed.reconcile_diff_count,
                oa_ticket_id=delivery.oa_ticket_id,
                excel_path=delivery.excel_path,
                word_path=delivery.word_path,
                notify_preview_path=delivery.notify_preview_path,
                summary_text=processed.summary_text,
                kpis=kpi_payload,
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

        logger.error("Workflow '%s' failed in %.2fs: %s", wf.key, elapsed, exc)
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


def run_backfill_workflow(
    wf: Workflow,
    days: int = 7,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    dry_run: bool | None = True,
    cfg: AppConfig | None = None,
    job_name: str | None = None,
) -> list[PipelineOutcome]:
    """Chronologically backfill N days (or date range) using the given workflow plugin."""
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
            run_workflow(
                wf=wf,
                biz_date=curr.isoformat(),
                force=force,
                dry_run=dry_run,
                cfg=cfg,
                job_name=job_name,
            )
        )
        curr += timedelta(days=1)
    return outcomes
