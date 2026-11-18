from __future__ import annotations

from datetime import date, datetime, timedelta

from core.config_loader import AppConfig, load_config
from core.live_observer import LiveRunTracker
from core.runner import PipelineOutcome, run_workflow
from workflows.registry import get_workflow

__all__ = [
    "PipelineOutcome",
    "run_daily_pipeline",
    "run_backfill_pipeline",
]


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
    """Resolve the configured Workflow plugin for the target job and run it."""
    cfg = cfg or load_config()
    job_def = cfg.get_job(job_name)
    wf = get_workflow(job_def.workflow)
    return run_workflow(
        wf=wf,
        biz_date=biz_date,
        force=force,
        dry_run=dry_run,
        cfg=cfg,
        simulate_failure=simulate_failure,
        job_name=job_def.job_name,
        observer=observer,
        session_id=session_id,
        visual_delay_ms=visual_delay_ms,
        fresh_login=fresh_login,
        headless_override=headless_override,
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
