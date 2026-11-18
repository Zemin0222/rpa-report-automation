from __future__ import annotations

from collections import deque
from datetime import date
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any

from fastapi import HTTPException, Request

from core.config_loader import AppConfig
from core.logger import get_logger
from core.pipeline import run_backfill_pipeline, run_daily_pipeline
from mock_systems.invoice_factory import populate_sample_invoices
from storage.state_db import StateDB

logger = get_logger("rpa.demo_guard")

AUTH_COOKIE_NAME = "rpa_console_auth"


def is_demo_mode() -> bool:
    """Return True when public guest demo guard & rate limiting are enabled."""
    return os.getenv("DEMO_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}


def get_demo_rate_limit() -> tuple[int, int]:
    """Return (max_requests, window_seconds) configured for guest demo triggers."""
    try:
        max_req = max(1, int(os.getenv("DEMO_RATE_LIMIT_MAX", "2")))
    except ValueError:
        max_req = 2
    try:
        win_sec = max(1, int(os.getenv("DEMO_RATE_LIMIT_WINDOW_SEC", "600")))
    except ValueError:
        win_sec = 600
    return max_req, win_sec


def auth_token_for(password: str) -> str:
    return hashlib.sha256(f"rpa_console::{password}".encode("utf-8")).hexdigest()


def is_admin_authenticated(request: Request) -> bool:
    """Validate console password cookie; in DEMO_MODE=1 admin requires an explicit DASHBOARD_PASSWORD."""
    required_pwd = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not required_pwd:
        return not is_demo_mode()
    cookie_val = request.cookies.get(AUTH_COOKIE_NAME, "")
    expected = auth_token_for(required_pwd)
    return bool(cookie_val) and hmac.compare_digest(cookie_val, expected)


def require_view_access(request: Request) -> None:
    """Allow public guest reads/demo triggers when DEMO_MODE=1; otherwise require cookie auth if set."""
    if is_demo_mode():
        return
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized: please sign in")


def require_admin_access(request: Request) -> None:
    """Protect mutating/destructive operations with 403 in DEMO_MODE or 401 in private mode."""
    if is_admin_authenticated(request):
        return
    if is_demo_mode():
        raise HTTPException(
            status_code=403,
            detail="访客演示模式受限：清理、备份、上传与外发告警等管理操作需先点击「管理员解锁」",
        )
    raise HTTPException(status_code=401, detail="Unauthorized: please sign in")


def get_client_ip(request: Request) -> str:
    """Resolve visitor client IP respecting reverse-proxy headers (Caddy / Nginx / Cloudflare)."""
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        first_hop = xff.split(",")[0].strip()
        if first_hop:
            return first_hop
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class SlidingWindowRateLimiter:
    """Thread-safe in-memory per-IP sliding window rate limiter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def consume(
        self,
        bucket: str,
        client_ip: str,
        limit: int,
        window_sec: int,
    ) -> tuple[bool, int, int]:
        """Try consuming 1 quota token; returns (allowed, remaining, retry_after_sec)."""
        now = time.monotonic()
        cutoff = now - window_sec
        key = (bucket, client_ip)
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                oldest = dq[0]
                retry_after = max(1, int(round((oldest + window_sec) - now)))
                return False, 0, retry_after
            dq.append(now)
            remaining = max(0, limit - len(dq))
            return True, remaining, 0


DEMO_LIMITER = SlidingWindowRateLimiter()


def enforce_demo_rate_limit(
    request: Request,
    *,
    bucket: str = "demo_run",
    limit: int | None = None,
    window_sec: int | None = None,
) -> None:
    """Enforce sliding-window IP quota for unauthenticated visitors when DEMO_MODE=1."""
    if not is_demo_mode() or is_admin_authenticated(request):
        return
    default_max, default_win = get_demo_rate_limit()
    eff_limit = limit if limit is not None else default_max
    eff_win = window_sec if window_sec is not None else default_win
    ip = get_client_ip(request)
    allowed, _, retry_after = DEMO_LIMITER.consume(bucket, ip, eff_limit, eff_win)
    if not allowed:
        minutes = max(1, eff_win // 60)
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail=(
                f"当前 IP 演示体验次数已达上限（每 {minutes} 分钟最多 {eff_limit} 次），"
                f"请约 {retry_after} 秒后再试；也可在右侧直接回放最近一次成功录像。"
            ),
        )


def _wipe_dir_contents(target_dir: Path | None) -> int:
    if target_dir is None or not target_dir.exists():
        return 0
    removed = 0
    for item in list(target_dir.iterdir()):
        if item.name == ".gitkeep":
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
        elif item.is_file():
            item.unlink(missing_ok=True)
            removed += 1
    return removed


def reset_demo_data(
    cfg: AppConfig,
    *,
    seed_days: int = 7,
    seed_jobs: bool = True,
) -> dict[str, Any]:
    """Nightly demo cleanup & fresh multi-day seed (preserves auth_states and AI cache)."""
    files_removed = 0
    for d in (
        cfg.storage.downloads,
        cfg.storage.outputs,
        cfg.storage.screenshots,
        cfg.storage.live_frames,
        cfg.storage.invoices,
    ):
        files_removed += _wipe_dir_contents(d)

    StateDB(cfg.storage.db_path).reset_demo_records(preserve_ai_cache=True)
    DEMO_LIMITER.reset()

    seeded_daily = 0
    expense_status = "SKIPPED"
    if seed_jobs:
        outcomes = run_backfill_pipeline(
            days=max(1, min(14, seed_days)),
            force=True,
            dry_run=True,
            cfg=cfg,
            job_name="daily_ops_report",
        )
        seeded_daily = sum(1 for o in outcomes if o.status == "SUCCESS")

        if cfg.storage.invoices is not None:
            populate_sample_invoices(
                cfg.storage.invoices,
                date.today().isoformat(),
                cfg.expense.company_name,
                cfg.expense.company_tax_id,
                overwrite=True,
            )
        exp_out = run_daily_pipeline(
            biz_date=date.today().isoformat(),
            force=True,
            dry_run=True,
            cfg=cfg,
            job_name="expense_reimburse",
        )
        expense_status = exp_out.status

    logger.info(
        "Demo data reset complete: cleared=%d entries, seeded_daily=%d, expense=%s",
        files_removed,
        seeded_daily,
        expense_status,
    )
    return {
        "cleared_entries": files_removed,
        "seeded_daily_days": seeded_daily,
        "expense_status": expense_status,
    }
