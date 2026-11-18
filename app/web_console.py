from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_loader import load_config
from core.doctor import run_doctor_checks
from core.housekeeping import cleanup_storage, get_storage_usage_mb
from core.live_observer import LiveRunTracker
from core.pipeline import run_backfill_pipeline, run_daily_pipeline
from notifiers.webhook import send_failure_alert
from storage.state_db import StateDB
from workflows.registry import get_workflow, list_workflows

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
CONSOLE_HTML_PATH = TEMPLATES_DIR / "console.html"
ALLOWED_DROPZONE_EXTS = {".csv", ".xlsx", ".xls", ".pdf"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

app = FastAPI(title="RPA 跨系统数据搬运与自动对账工作台")
_LIVE_TRACKERS: dict[str, LiveRunTracker] = {}


def _get_live_tracker(frames_root: Path) -> LiveRunTracker:
    key = str(frames_root.resolve())
    if key not in _LIVE_TRACKERS:
        _LIVE_TRACKERS[key] = LiveRunTracker(frames_root)
    return _LIVE_TRACKERS[key]


class LoginRequest(BaseModel):
    password: str


class RunRequest(BaseModel):
    job_name: str | None = None
    biz_date: str | None = None
    force: bool = False
    dry_run: bool = True
    simulate_failure: bool = False


class VisualRunRequest(BaseModel):
    job_name: str | None = None
    biz_date: str | None = None
    force: bool = True
    dry_run: bool = True
    fresh_login: bool = True
    visual_delay_ms: int = 400
    simulate_failure: bool = False


class BackfillRequest(BaseModel):
    job_name: str | None = None
    days: int = 7
    force: bool = False
    dry_run: bool = True


class CleanupRequest(BaseModel):
    dry_run: bool = True


class TestAlertRequest(BaseModel):
    job_name: str | None = None
    biz_date: str | None = None
    dry_run: bool = True


def _auth_token_for(password: str) -> str:
    return hashlib.sha256(f"rpa_console::{password}".encode("utf-8")).hexdigest()


def _require_auth(request: Request) -> None:
    required_pwd = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not required_pwd:
        return
    cookie_val = request.cookies.get("rpa_console_auth", "")
    expected = _auth_token_for(required_pwd)
    if not hmac.compare_digest(cookie_val, expected):
        raise HTTPException(status_code=401, detail="Unauthorized: please sign in")


def _safe_child_path(base_dir: Path, filename: str) -> Path:
    clean_name = Path(filename).name
    if not clean_name or clean_name in {".", ".."} or clean_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    candidate = (base_dir / clean_name).resolve()
    if not candidate.is_relative_to(base_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid path")
    return candidate


def _validate_date(biz_date: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", biz_date):
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")
    return biz_date


@app.get("/health")
@app.get("/api/health")
@app.get("/_stcore/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth-status")
def auth_status(request: Request) -> dict[str, Any]:
    required_pwd = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not required_pwd:
        return {"auth_required": False, "authenticated": True}
    cookie_val = request.cookies.get("rpa_console_auth", "")
    authed = hmac.compare_digest(cookie_val, _auth_token_for(required_pwd))
    return {"auth_required": True, "authenticated": authed}


@app.post("/api/login")
def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    required_pwd = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not required_pwd:
        return {"authenticated": True}
    if not hmac.compare_digest(payload.password, required_pwd):
        raise HTTPException(status_code=401, detail="访问口令错误，请检查 DASHBOARD_PASSWORD 配置")
    response.set_cookie(
        key="rpa_console_auth",
        value=_auth_token_for(required_pwd),
        httponly=True,
        samesite="lax",
        max_age=7 * 86400,
    )
    return {"authenticated": True}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("rpa_console_auth")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not CONSOLE_HTML_PATH.exists():
        raise HTTPException(status_code=500, detail="console.html template missing")
    return HTMLResponse(CONSOLE_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/state")
def get_state(
    request: Request,
    job_name: str | None = None,
    biz_date: str | None = None,
) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    db = StateDB(cfg.storage.db_path)

    job_objs = cfg.jobs if cfg.jobs else [cfg.get_job()]
    available_jobs = [j.job_name for j in job_objs]
    selected_job = job_name if (job_name and job_name in available_jobs) else available_jobs[0]
    job_def = cfg.get_job(selected_job)

    jobs_meta: list[dict[str, Any]] = []
    for jd in job_objs:
        try:
            wf_obj = get_workflow(jd.workflow)
            wf_title, wf_desc, wf_view = wf_obj.title, wf_obj.description, wf_obj.view_type
        except ValueError:
            wf_title, wf_desc, wf_view = jd.job_name, "", "generic"
        jobs_meta.append(
            {
                "job_name": jd.job_name,
                "workflow": jd.workflow,
                "title": jd.title or wf_title,
                "description": jd.description or wf_desc,
                "view_type": wf_view,
                "enabled": jd.enabled,
            }
        )

    active_wf_meta = next((m for m in jobs_meta if m["job_name"] == selected_job), jobs_meta[0])

    all_runs = db.list_recent_runs(limit=80)
    job_runs = [r for r in all_runs if r.get("job_name") == selected_job]
    success_runs = [r for r in job_runs if r.get("status") == "SUCCESS" and r.get("kpi_json")]

    available_dates: list[str] = []
    seen_dates: set[str] = set()
    for r in sorted(success_runs, key=lambda x: (x["biz_date"], x["id"]), reverse=True):
        d = str(r["biz_date"])
        if d not in seen_dates:
            seen_dates.add(d)
            available_dates.append(d)

    selected_date: str | None = None
    if biz_date and biz_date in seen_dates:
        selected_date = biz_date
    elif available_dates:
        selected_date = available_dates[0]

    active_run_payload: dict[str, Any] | None = None
    notify_card: dict[str, Any] | None = None

    if selected_date:
        matched_run = next((r for r in success_runs if r["biz_date"] == selected_date), None)
        if matched_run:
            kpi_data = json.loads(matched_run["kpi_json"])
            excel_p = Path(matched_run["excel_path"]) if matched_run.get("excel_path") else None
            word_p = Path(matched_run["word_path"]) if matched_run.get("word_path") else None
            notify_p = (
                Path(matched_run["notify_preview_path"])
                if matched_run.get("notify_preview_path")
                else None
            )
            active_run_payload = {
                "run_id": int(matched_run["id"]),
                "biz_date": selected_date,
                "workflow_key": active_wf_meta["workflow"],
                "view_type": active_wf_meta["view_type"],
                "started_at": matched_run.get("started_at"),
                "duration_sec": float(matched_run.get("duration_sec") or 0.0),
                "row_count": int(matched_run.get("row_count") or 0),
                "anomaly_count": int(matched_run.get("anomaly_count") or 0),
                "summary_text": matched_run.get("summary_text") or "",
                "kpi": kpi_data,
                "has_excel": bool(excel_p and excel_p.exists()),
                "has_word": bool(word_p and word_p.exists()),
                "has_notify": bool(notify_p and notify_p.exists()),
                "steps": db.list_steps_for_run(int(matched_run["id"])),
            }
            if notify_p and notify_p.exists():
                try:
                    notify_card = json.loads(notify_p.read_text(encoding="utf-8"))
                except Exception:
                    notify_card = None

    latest_run_steps: list[dict[str, Any]] = []
    if job_runs:
        latest_run_steps = db.list_steps_for_run(int(job_runs[0]["id"]))

    runs_table = [
        {
            "id": int(r["id"]),
            "job_name": r["job_name"],
            "biz_date": r["biz_date"],
            "status": r["status"],
            "started_at": r.get("started_at") or "",
            "finished_at": r.get("finished_at") or "",
            "duration_sec": float(r.get("duration_sec") or 0.0),
            "row_count": int(r.get("row_count") or 0),
            "anomaly_count": int(r.get("anomaly_count") or 0),
            "error_message": r.get("error_message") or "",
            "screenshot_name": Path(r["screenshot_path"]).name if r.get("screenshot_path") else None,
        }
        for r in job_runs[:30]
    ]

    screenshots = []
    for shot in sorted(cfg.storage.screenshots.glob("*.png"), reverse=True)[:16]:
        st_info = shot.stat()
        screenshots.append(
            {
                "name": shot.name,
                "size_kb": round(st_info.st_size / 1024, 1),
                "modified_at": datetime.fromtimestamp(st_info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    dropzone_files = []
    for item in sorted(cfg.storage.dropzone.iterdir(), key=lambda p: p.name.lower()):
        if item.is_file() and not item.name.startswith("."):
            st_info = item.stat()
            dropzone_files.append(
                {
                    "name": item.name,
                    "size_kb": round(st_info.st_size / 1024, 1),
                    "ext": item.suffix.lower().lstrip("."),
                    "modified_at": datetime.fromtimestamp(st_info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    return {
        "today": date.today().isoformat(),
        "jobs": available_jobs,
        "jobs_meta": jobs_meta,
        "workflows": [
            {
                "key": w.key,
                "title": w.title,
                "description": w.description,
                "view_type": w.view_type,
            }
            for w in list_workflows()
        ],
        "active_workflow": active_wf_meta,
        "selected_job": selected_job,
        "available_dates": available_dates,
        "selected_date": selected_date,
        "active_run": active_run_payload,
        "notify_card": notify_card,
        "trend": db.get_daily_trend(selected_job, limit_days=14),
        "runs": runs_table,
        "latest_run_steps": latest_run_steps,
        "screenshots": screenshots,
        "dropzone_files": dropzone_files,
        "doctor": run_doctor_checks(cfg),
        "storage_usage": get_storage_usage_mb(cfg.storage),
        "config_summary": {
            "headless": cfg.browser.headless,
            "timezone": cfg.timezone,
            "daily_cron": job_def.daily_cron,
            "weekly_cron": job_def.weekly_cron,
            "dry_run_default": cfg.notifier.dry_run,
            "channel": cfg.notifier.channel,
            "mock_base_url": cfg.connectors.mock_crm.base_url,
            "thresholds": cfg.thresholds.model_dump(),
            "retention": cfg.retention.model_dump(),
            "sources": [
                {"id": s.id, "connector": s.connector, "role": s.role, "enabled": s.enabled}
                for s in job_def.sources
            ],
        },
    }


@app.get("/api/runs/{run_id}/steps")
def get_run_steps(run_id: int, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    db = StateDB(cfg.storage.db_path)
    return {"run_id": run_id, "steps": db.list_steps_for_run(run_id)}


@app.post("/api/run")
def trigger_run(payload: RunRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    outcome = run_daily_pipeline(
        biz_date=payload.biz_date,
        force=payload.force,
        dry_run=payload.dry_run,
        cfg=cfg,
        simulate_failure=payload.simulate_failure,
        job_name=payload.job_name,
    )
    return {
        "run_id": outcome.run_id,
        "biz_date": outcome.biz_date,
        "status": outcome.status,
        "duration_sec": outcome.duration_sec,
        "row_count": outcome.row_count,
        "anomaly_count": outcome.anomaly_count,
        "reconcile_diff_count": outcome.reconcile_diff_count,
        "oa_ticket_id": outcome.oa_ticket_id,
        "screenshot_name": outcome.screenshot_path.name if outcome.screenshot_path else None,
        "error_message": outcome.error_message,
    }


@app.post("/api/run-visual")
def trigger_visual_run(payload: VisualRunRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    assert cfg.storage.live_frames is not None
    tracker = _get_live_tracker(cfg.storage.live_frames)
    target_date = payload.biz_date or date.today().isoformat()
    job_name = payload.job_name or cfg.job_name
    session_id = tracker.create_session(job_name, target_date)

    thread = threading.Thread(
        target=run_daily_pipeline,
        kwargs={
            "biz_date": target_date,
            "force": payload.force,
            "dry_run": payload.dry_run,
            "cfg": cfg,
            "simulate_failure": payload.simulate_failure,
            "job_name": job_name,
            "observer": tracker,
            "session_id": session_id,
            "visual_delay_ms": max(0, min(1500, payload.visual_delay_ms)),
            "fresh_login": payload.fresh_login,
        },
        daemon=True,
    )
    thread.start()
    return {"session_id": session_id, "status": "RUNNING"}


@app.get("/api/live/latest")
def get_latest_live_session(request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    assert cfg.storage.live_frames is not None
    tracker = _get_live_tracker(cfg.storage.live_frames)
    sess = tracker.get_session_dict(None)
    return {"session": sess}


@app.get("/api/live/{session_id}")
def get_live_session(session_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    assert cfg.storage.live_frames is not None
    tracker = _get_live_tracker(cfg.storage.live_frames)
    sess = tracker.get_session_dict(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Live session not found")
    return {"session": sess}


@app.get("/api/live/frame/{session_id}/{filename}")
def get_live_frame(session_id: str, filename: str, request: Request) -> FileResponse:
    _require_auth(request)
    cfg = load_config()
    assert cfg.storage.live_frames is not None
    sess_dir = _safe_child_path(cfg.storage.live_frames, session_id)
    frame_path = _safe_child_path(sess_dir, filename)
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(path=frame_path, media_type="image/jpeg", filename=frame_path.name)


@app.post("/api/backfill")
def trigger_backfill(payload: BackfillRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    days = max(1, min(30, payload.days))
    outcomes = run_backfill_pipeline(
        days=days,
        force=payload.force,
        dry_run=payload.dry_run,
        cfg=cfg,
        job_name=payload.job_name,
    )
    return {
        "total": len(outcomes),
        "success": sum(1 for o in outcomes if o.status == "SUCCESS"),
        "skipped": sum(1 for o in outcomes if o.status == "SKIPPED"),
        "locked": sum(1 for o in outcomes if o.status == "LOCKED"),
        "failed": sum(1 for o in outcomes if o.status == "FAILED"),
    }


@app.post("/api/ops/cleanup")
def ops_cleanup(payload: CleanupRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    summary = cleanup_storage(cfg.storage, cfg.retention, dry_run=payload.dry_run)
    return {"dry_run": payload.dry_run, **summary}


@app.post("/api/ops/backup")
def ops_backup(request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    db = StateDB(cfg.storage.db_path)
    assert cfg.storage.backups is not None
    bkp_path = db.backup_database(cfg.storage.backups, keep_count=7)
    return {"filename": bkp_path.name, "size_kb": round(bkp_path.stat().st_size / 1024, 1)}


@app.post("/api/ops/test-alert")
def ops_test_alert(payload: TestAlertRequest, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    target_date = payload.biz_date or date.today().isoformat()
    job_name = payload.job_name or cfg.job_name
    alert_path = send_failure_alert(
        cfg.notifier,
        job_name=job_name,
        biz_date=target_date,
        error_message="[控制台联调测试] 验证飞书/钉钉加签通知与故障告警卡片渲染。",
        screenshot_path=None,
        output_dir=cfg.storage.outputs,
        force_dry_run=payload.dry_run,
    )
    card_data = json.loads(alert_path.read_text(encoding="utf-8"))
    return {"filename": alert_path.name, "card": card_data}


@app.post("/api/dropzone/upload")
def upload_dropzone_file(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    clean_name = Path(file.filename).name
    if Path(clean_name).suffix.lower() not in ALLOWED_DROPZONE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式，请上传 {', '.join(sorted(ALLOWED_DROPZONE_EXTS))}",
        )
    content = file.file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 25MB 限制")

    dest = _safe_child_path(cfg.storage.dropzone, clean_name)
    dest.write_bytes(content)
    return {"filename": clean_name, "size_kb": round(len(content) / 1024, 1)}


@app.delete("/api/dropzone/{filename}")
def delete_dropzone_file(filename: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    target = _safe_child_path(cfg.storage.dropzone, filename)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    target.unlink()
    return {"deleted": filename}


@app.get("/api/download/{kind}/{biz_date}")
def download_artifact(
    kind: Literal["excel", "word", "notify", "alert"],
    biz_date: str,
    request: Request,
) -> FileResponse:
    _require_auth(request)
    clean_date = _validate_date(biz_date)
    cfg = load_config()
    file_map = {
        "excel": (
            f"daily_report_{clean_date}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        "word": (
            f"weekly_report_{clean_date}.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        "notify": (f"notify_preview_{clean_date}.json", "application/json"),
        "alert": (f"alert_preview_{clean_date}.json", "application/json"),
    }
    filename, media_type = file_map[kind]
    target = _safe_child_path(cfg.storage.outputs, filename)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return FileResponse(path=target, media_type=media_type, filename=filename)


@app.get("/api/screenshots/{filename}")
def get_screenshot(filename: str, request: Request) -> FileResponse:
    _require_auth(request)
    cfg = load_config()
    target = _safe_child_path(cfg.storage.screenshots, filename)
    if not target.exists() or target.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(path=target, media_type="image/png", filename=target.name)
