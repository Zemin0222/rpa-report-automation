from __future__ import annotations

import argparse
from datetime import date
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from core.browser import BrowserManager
from core.config_loader import AppConfig, load_config
from core.doctor import run_doctor_checks
from core.housekeeping import cleanup_storage
from core.logger import get_logger
from core.pipeline import run_backfill_pipeline, run_daily_pipeline
from mock_systems.server import run_mock_server

logger = get_logger("rpa.cli")


def resolve_auth_login_url(cfg: AppConfig, site: str, explicit_url: str | None) -> str:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    presets = {
        "mock_crm": f"{cfg.connectors.mock_crm.base_url.rstrip('/')}/crm/login",
        "mock_ads": f"{cfg.connectors.mock_ads.base_url.rstrip('/')}/ads/login",
        "mock_finance": f"{cfg.connectors.mock_finance.base_url.rstrip('/')}/finance/login",
        "mock_oa": f"{cfg.connectors.mock_oa.base_url.rstrip('/')}/oa/login",
        "mock_expense": f"{cfg.connectors.mock_expense.base_url.rstrip('/')}/expense/login",
    }
    if site in presets:
        return presets[site]
    raise ValueError(f"Unknown site preset '{site}'. Please pass --url explicitly.")


def cmd_run_once(args: argparse.Namespace) -> int:
    dry_run = None
    if args.dry_run:
        dry_run = True
    elif args.no_dry_run:
        dry_run = False

    outcome = run_daily_pipeline(
        biz_date=args.date,
        force=args.force,
        dry_run=dry_run,
        simulate_failure=args.simulate_failure,
        job_name=args.job,
        visual_delay_ms=450 if args.watch else 0,
        fresh_login=args.fresh_login,
        headless_override=False if args.watch else None,
    )
    logger.info(
        "Run result: status=%s run_id=%d date=%s rows=%d anomalies=%d diffs=%d oa_ticket=%s",
        outcome.status,
        outcome.run_id,
        outcome.biz_date,
        outcome.row_count,
        outcome.anomaly_count,
        outcome.reconcile_diff_count,
        outcome.oa_ticket_id or "-",
    )
    if outcome.status in {"FAILED", "LOCKED"}:
        logger.error("Run did not complete: %s", outcome.error_message)
        return 1
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    outcomes = run_backfill_pipeline(
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        force=args.force,
        dry_run=not args.no_dry_run,
        job_name=args.job,
    )
    failed = [o for o in outcomes if o.status == "FAILED"]
    logger.info(
        "Backfill finished: total=%d success=%d skipped=%d failed=%d",
        len(outcomes),
        sum(1 for o in outcomes if o.status == "SUCCESS"),
        sum(1 for o in outcomes if o.status == "SKIPPED"),
        len(failed),
    )
    return 1 if failed else 0


def cmd_auth_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    login_url = resolve_auth_login_url(cfg, args.site, args.url)
    mgr = BrowserManager(
        auth_dir=cfg.storage.auth_states,
        screenshot_dir=cfg.storage.screenshots,
        download_dir=cfg.storage.downloads,
        headless=False,
    )
    saved = mgr.interactive_login(args.site, login_url)
    logger.info("Saved interactive auth state -> %s", saved)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    cfg = load_config()
    summary = cleanup_storage(cfg.storage, cfg.retention, dry_run=args.dry_run)
    logger.info("Cleanup summary: %s", summary)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    cfg = load_config()
    checks = run_doctor_checks(cfg)
    has_error = False
    for item in checks:
        status = item["状态"]
        if status == "ERROR":
            has_error = True
        print(f"[{status:<5}] {item['检查项']}: {item['详情']}")
    return 1 if has_error else 0


def cmd_schedule(_: argparse.Namespace) -> int:
    cfg = load_config()
    scheduler = BlockingScheduler(timezone=cfg.timezone)

    jobs_to_register = [j for j in cfg.jobs if j.enabled] or [cfg.get_job()]
    for job_def in jobs_to_register:
        j_name = job_def.job_name
        daily_trigger = CronTrigger.from_crontab(job_def.daily_cron, timezone=cfg.timezone)
        scheduler.add_job(
            lambda jn=j_name: run_daily_pipeline(biz_date=date.today().isoformat(), force=False, job_name=jn),
            trigger=daily_trigger,
            id=f"{j_name}_daily",
            name=f"{j_name} Daily Report",
            replace_existing=True,
        )

        weekly_trigger = CronTrigger.from_crontab(job_def.weekly_cron, timezone=cfg.timezone)
        scheduler.add_job(
            lambda jn=j_name: run_daily_pipeline(biz_date=date.today().isoformat(), force=True, job_name=jn),
            trigger=weekly_trigger,
            id=f"{j_name}_weekly",
            name=f"{j_name} Weekly Report",
            replace_existing=True,
        )
        logger.info(
            "Registered job '%s' (timezone=%s, daily='%s', weekly='%s')",
            j_name,
            cfg.timezone,
            job_def.daily_cron,
            job_def.weekly_cron,
        )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
    return 0


def cmd_mock_server(args: argparse.Namespace) -> int:
    logger.info("Starting mock CRM, Ads & Finance systems at http://%s:%d", args.host, args.port)
    run_mock_server(host=args.host, port=args.port)
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    import uvicorn

    logger.info("Starting RPA Web Operations Console at http://%s:%d", args.host, args.port)
    uvicorn.run("app.web_console:app", host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-System RPA Report Automation CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run-once", help="Execute the daily report pipeline once")
    p_run.add_argument("--job", default=None, help="Job name defined in config/jobs.yaml")
    p_run.add_argument("--date", default=None, help="Business date (YYYY-MM-DD)")
    p_run.add_argument("--force", action="store_true", help="Ignore idempotency lock and rerun")
    p_run.add_argument("--dry-run", action="store_true", help="Force Dry-Run notification preview")
    p_run.add_argument("--no-dry-run", action="store_true", help="Enable real webhook/email delivery")
    p_run.add_argument(
        "--simulate-failure",
        action="store_true",
        help="Simulate a redesigned selector timeout to demo auto-screenshot & failure audit",
    )
    p_run.add_argument(
        "--watch",
        action="store_true",
        help="Open a visible Chrome window on desktop and slow-mo highlight every RPA action",
    )
    p_run.add_argument(
        "--fresh-login",
        action="store_true",
        help="Ignore saved auth_states so login form filling is visibly executed",
    )
    p_run.set_defaults(func=cmd_run_once)

    p_bf = sub.add_parser("backfill", help="Backfill historical daily reports chronologically")
    p_bf.add_argument("--job", default=None, help="Job name defined in config/jobs.yaml")
    p_bf.add_argument("--days", type=int, default=7, help="Number of days ending today (default: 7)")
    p_bf.add_argument("--start-date", default=None, help="Start date (YYYY-MM-DD)")
    p_bf.add_argument("--end-date", default=None, help="End date (YYYY-MM-DD)")
    p_bf.add_argument("--force", action="store_true", help="Overwrite existing successful dates")
    p_bf.add_argument("--no-dry-run", action="store_true", help="Send real notifications during backfill")
    p_bf.set_defaults(func=cmd_backfill)

    p_auth = sub.add_parser("auth-login", help="Open headful browser for manual login & save storage_state")
    p_auth.add_argument("--site", required=True, help="Site key (e.g. mock_crm, my_erp)")
    p_auth.add_argument("--url", default=None, help="Target login URL (optional for mock_* presets)")
    p_auth.set_defaults(func=cmd_auth_login)

    p_clean = sub.add_parser("cleanup", help="Purge expired downloads, screenshots, logs, and old outputs")
    p_clean.add_argument("--dry-run", action="store_true", help="Preview files to remove without deleting")
    p_clean.set_defaults(func=cmd_cleanup)

    p_doc = sub.add_parser("doctor", help="Verify runtime, DB WAL, auth states, disk space & secrets")
    p_doc.set_defaults(func=cmd_doctor)

    p_sched = sub.add_parser("schedule", help="Start APScheduler cron daemon")
    p_sched.set_defaults(func=cmd_schedule)

    p_mock = sub.add_parser("mock-server", help="Start local FastAPI mock systems")
    p_mock.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_mock.add_argument("--port", type=int, default=8765, help="Bind port")
    p_mock.set_defaults(func=cmd_mock_server)

    p_con = sub.add_parser("console", help="Start FastAPI SPA Web Operations Console")
    p_con.add_argument("--host", default="0.0.0.0", help="Bind host")
    p_con.add_argument("--port", type=int, default=8501, help="Bind port")
    p_con.set_defaults(func=cmd_console)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
