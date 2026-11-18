from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import app.demo_guard as dg
import app.web_console as wc
from core.config_loader import StoragePaths, load_config
from core.job_queue import QueueFullError, SingleWorkerQueue
from core.pipeline import PipelineOutcome
from storage.state_db import StateDB


def _isolated_cfg(tmp_path: Path):
    cfg = load_config()
    cfg.storage = StoragePaths(
        root=tmp_path,
        db_path=tmp_path / "state.db",
        auth_states=tmp_path / "auth_states",
        downloads=tmp_path / "downloads",
        outputs=tmp_path / "outputs",
        screenshots=tmp_path / "screenshots",
        dropzone=tmp_path / "dropzone",
        logs=tmp_path / "logs",
        backups=tmp_path / "backups",
        live_frames=tmp_path / "live_frames",
        invoices=tmp_path / "invoices",
    )
    cfg.storage.ensure_all()
    return cfg


def test_single_worker_queue_serializes_concurrent_tasks_and_reports_eta() -> None:
    q = SingleWorkerQueue(max_queue_size=3, default_estimate_sec=5.0)
    gate = threading.Event()
    active_lock = threading.Lock()
    active_now = 0
    max_active = 0
    order: list[int] = []

    def make_task(idx: int, wait_gate: bool = False):
        def _run():
            nonlocal active_now, max_active
            with active_lock:
                active_now += 1
                max_active = max(max_active, active_now)
            try:
                if wait_gate:
                    assert gate.wait(timeout=5.0)
                time.sleep(0.03)
                order.append(idx)
                return idx
            finally:
                with active_lock:
                    active_now -= 1

        return _run

    t1, pos1, ahead1, est1 = q.enqueue(make_task(1, wait_gate=True), kind="visual", biz_date="2026-11-18")
    assert ahead1 == 0
    assert pos1 == 0
    assert est1 == 0

    # Ensure t1 has popped onto the worker thread and is actively blocking
    deadline = time.perf_counter() + 2.0
    while q.get_status(t1.ticket_id)["ticket"]["status"] != "RUNNING":
        assert time.perf_counter() < deadline
        time.sleep(0.01)

    t2, pos2, ahead2, est2 = q.enqueue(make_task(2), kind="visual", biz_date="2026-11-18")
    t3, pos3, ahead3, est3 = q.enqueue(make_task(3), kind="visual", biz_date="2026-11-18")
    t4, _, _, _ = q.enqueue(make_task(4), kind="visual", biz_date="2026-11-18")

    assert ahead2 == 1 and pos2 == 1 and est2 >= 1
    assert ahead3 == 2 and pos3 == 2 and est3 >= est2
    assert q.get_status(t3.ticket_id)["ticket"]["ahead_count"] == 2
    assert q.get_status()["queued_count"] == 3

    # Waiting queue capacity = 3 (t2, t3, t4), next enqueue must reject cleanly
    with pytest.raises(QueueFullError):
        q.enqueue(make_task(5))

    gate.set()
    for t in (t1, t2, t3, t4):
        assert t.done_event.wait(timeout=5.0)
        assert t.status == "DONE"

    assert max_active == 1
    assert order == [1, 2, 3, 4]
    assert q.get_status()["busy"] is False


def test_demo_mode_guest_readonly_vs_admin_unlock(tmp_path: Path, monkeypatch) -> None:
    cfg = _isolated_cfg(tmp_path)
    dg.DEMO_LIMITER.reset()
    monkeypatch.setattr(wc, "load_config", lambda: cfg)
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "admin_secret")

    client = TestClient(wc.app)

    # Guest can read without logging in
    r_auth = client.get("/api/auth-status")
    assert r_auth.status_code == 200
    assert r_auth.json()["demo_mode"] is True
    assert r_auth.json()["auth_required"] is False
    assert r_auth.json()["is_admin"] is False

    assert client.get("/api/state").status_code == 200
    assert client.get("/api/queue/status").status_code == 200

    # Admin-only mutating/maintenance endpoints return 403 for guests
    assert client.post("/api/ops/cleanup", json={"dry_run": True}).status_code == 403
    assert client.post("/api/ops/backup").status_code == 403
    assert client.post("/api/ops/test-alert", json={"dry_run": True}).status_code == 403
    assert client.post("/api/ops/demo-reset").status_code == 403
    assert client.post("/api/backfill", json={"days": 3}).status_code == 403
    assert (
        client.post(
            "/api/dropzone/upload",
            files={"file": ("hack.csv", b"a,b\n1,2\n", "text/csv")},
        ).status_code
        == 403
    )
    assert client.delete("/api/dropzone/hack.csv").status_code == 403
    assert (
        client.post(
            "/api/expense/upload",
            files={"file": ("inv.pdf", b"%PDF-1.4\n", "application/pdf")},
        ).status_code
        == 403
    )
    assert client.delete("/api/expense/files/inv.pdf").status_code == 403

    # Admin unlocks via DASHBOARD_PASSWORD
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "admin_secret"}).status_code == 200
    assert client.get("/api/auth-status").json()["is_admin"] is True
    assert client.post("/api/ops/backup").status_code == 200

    # Logout restores guest restriction
    assert client.post("/api/logout").status_code == 200
    assert client.post("/api/ops/backup").status_code == 403


def test_demo_mode_ip_rate_limit_and_concurrent_visual_queue(tmp_path: Path, monkeypatch) -> None:
    cfg = _isolated_cfg(tmp_path)
    dg.DEMO_LIMITER.reset()
    test_queue = SingleWorkerQueue(max_queue_size=10, default_estimate_sec=8.0)

    assert cfg.storage.live_frames is not None
    tracker = wc._get_live_tracker(cfg.storage.live_frames)
    # Seed a completed live session so fallback replay is populated for queued visitors
    done_sid = tracker.create_session("daily_ops_report", "2026-11-17")
    tracker.record_event(done_sid, "完成", "CLICK", "#done", "已完成", frame_bytes=b"\xff\xd8\xff\xe0OK")
    tracker.finish_session(done_sid, "SUCCESS", {"oa_ticket_id": "OA-20261117-001"})

    monkeypatch.setattr(wc, "load_config", lambda: cfg)
    monkeypatch.setattr(wc, "_JOB_QUEUE", test_queue)
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "admin_secret")
    monkeypatch.setenv("DEMO_RATE_LIMIT_MAX", "2")
    monkeypatch.setenv("DEMO_RATE_LIMIT_WINDOW_SEC", "600")

    first_gate = threading.Event()
    active_lock = threading.Lock()
    active = 0
    max_overlap = 0
    seen_calls: list[dict[str, object]] = []

    def fake_run_daily_pipeline(**kwargs):
        nonlocal active, max_overlap
        with active_lock:
            active += 1
            max_overlap = max(max_overlap, active)
        try:
            call_idx = len(seen_calls) + 1
            seen_calls.append({"dry_run": kwargs.get("dry_run"), "idx": call_idx})
            if call_idx == 1:
                assert first_gate.wait(timeout=5.0)
            sid = kwargs.get("session_id")
            if sid and kwargs.get("observer"):
                kwargs["observer"].finish_session(sid, "SUCCESS", {"run_id": call_idx})
            return PipelineOutcome(
                run_id=call_idx,
                biz_date=str(kwargs.get("biz_date") or "2026-11-18"),
                status="SUCCESS",
                duration_sec=0.1,
                row_count=10,
                anomaly_count=0,
            )
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(wc, "run_daily_pipeline", fake_run_daily_pipeline)
    client = TestClient(wc.app)

    # Visitor A trigger #1 -> runs immediately; server coerces dry_run=True for guest
    r1 = client.post(
        "/api/run-visual",
        json={"job_name": "daily_ops_report", "dry_run": False},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r1.status_code == 200
    j1 = r1.json()
    assert j1["status"] == "RUNNING"
    assert j1["ahead_count"] == 0

    # Wait until worker starts run #1
    deadline = time.perf_counter() + 2.0
    while len(seen_calls) < 1:
        assert time.perf_counter() < deadline
        time.sleep(0.01)

    # Visitor B trigger #1 while Visitor A runs -> QUEUED + fallback_session attached
    r2 = client.post(
        "/api/run-visual",
        json={"job_name": "daily_ops_report"},
        headers={"X-Forwarded-For": "203.0.113.20"},
    )
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["status"] == "QUEUED"
    assert j2["ahead_count"] == 1
    assert j2["estimated_wait_sec"] > 0
    assert j2["fallback_session"]["session_id"] == done_sid

    # Visitor A trigger #2 -> allowed, QUEUED at position 2
    r3 = client.post(
        "/api/run-visual",
        json={"job_name": "daily_ops_report"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r3.status_code == 200
    assert r3.json()["ahead_count"] == 2

    # Visitor A trigger #3 -> rate limited with 429 + Retry-After header
    r4 = client.post(
        "/api/run-visual",
        json={"job_name": "daily_ops_report"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r4.status_code == 429
    assert int(r4.headers["Retry-After"]) >= 1

    # Visitor C trigger #1 -> separate IP bucket, succeeds in queue
    r5 = client.post(
        "/api/run-visual",
        json={"job_name": "daily_ops_report"},
        headers={"X-Forwarded-For": "203.0.113.30"},
    )
    assert r5.status_code == 200
    assert r5.json()["ahead_count"] == 3

    first_gate.set()
    deadline = time.perf_counter() + 5.0
    while test_queue.get_status()["busy"]:
        assert time.perf_counter() < deadline
        time.sleep(0.02)

    assert len(seen_calls) == 4
    assert all(c["dry_run"] is True for c in seen_calls)
    assert max_overlap == 1


def test_reset_demo_data_clears_transactional_state_and_keeps_auth_states(tmp_path: Path) -> None:
    cfg = _isolated_cfg(tmp_path)
    db = StateDB(cfg.storage.db_path)
    rid = db.start_run("daily_ops_report", "2026-11-18")
    db.finish_success(rid, 1.0, 5, 0, None, None, None, "ok", {})
    db.put_ai_cache("k1", "daily_summary", "gpt-4o-mini", '{"highlights":[],"suggestions":[]}', 50)
    db.record_ai_usage("2026-11-18", "daily_summary", "ai", "gpt-4o-mini", 20, 30, 50, 12.0)

    (cfg.storage.outputs / "daily_report_2026-11-18.xlsx").write_bytes(b"fake")
    (cfg.storage.downloads / "orders.csv").write_bytes(b"id\n1\n")
    (cfg.storage.outputs / ".gitkeep").write_text("", encoding="utf-8")
    (cfg.storage.auth_states / "mock_crm.json").write_text('{"cookies":[]}', encoding="utf-8")

    summary = dg.reset_demo_data(cfg, seed_jobs=False)
    assert summary["cleared_entries"] >= 2
    assert db.list_recent_runs(limit=10) == []
    assert db.get_ai_cache("k1") is not None
    assert (cfg.storage.outputs / ".gitkeep").exists()
    assert not (cfg.storage.outputs / "daily_report_2026-11-18.xlsx").exists()
    assert (cfg.storage.auth_states / "mock_crm.json").exists()
