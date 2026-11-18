from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient

import app.web_console as wc
from core.config_loader import StoragePaths, load_config
from core.live_observer import LiveRunTracker


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
    )
    cfg.storage.ensure_all()
    return cfg


def test_web_console_routes_and_ops(tmp_path: Path, monkeypatch) -> None:
    cfg = _isolated_cfg(tmp_path)
    monkeypatch.setattr(wc, "load_config", lambda: cfg)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    client = TestClient(wc.app)

    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "RPA 账务工作台" in r_home.text
    assert "${" in r_home.text  # JS template literals remain inside <script> cleanly

    assert client.get("/_stcore/health").status_code == 200
    assert client.get("/api/health").status_code == 200

    r_state = client.get("/api/state")
    assert r_state.status_code == 200
    payload = r_state.json()
    assert "jobs" in payload and "doctor" in payload and "storage_usage" in payload

    r_bkp = client.post("/api/ops/backup")
    assert r_bkp.status_code == 200
    assert r_bkp.json()["filename"].startswith("state_backup_")

    r_clean = client.post("/api/ops/cleanup", json={"dry_run": True})
    assert r_clean.status_code == 200
    assert r_clean.json()["dry_run"] is True

    r_alert = client.post("/api/ops/test-alert", json={"biz_date": "2026-11-17", "dry_run": True})
    assert r_alert.status_code == 200
    assert r_alert.json()["filename"] == "alert_preview_2026-11-17.json"

    r_up = client.post(
        "/api/dropzone/upload",
        files={"file": ("offline.csv", b"biz_date,channel,extra_cost\n2026-11-17,\xe6\x8a\x96\xe9\x9f\xb3,10\n", "text/csv")},
    )
    assert r_up.status_code == 200
    assert (cfg.storage.dropzone / "offline.csv").exists()

    r_del = client.delete("/api/dropzone/offline.csv")
    assert r_del.status_code == 200
    assert not (cfg.storage.dropzone / "offline.csv").exists()


def test_web_console_password_gate(tmp_path: Path, monkeypatch) -> None:
    cfg = _isolated_cfg(tmp_path)
    monkeypatch.setattr(wc, "load_config", lambda: cfg)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret_123")

    client = TestClient(wc.app)
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "secret_123"}).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_live_observer_and_frame_endpoint(tmp_path: Path, monkeypatch) -> None:
    cfg = _isolated_cfg(tmp_path)
    monkeypatch.setattr(wc, "load_config", lambda: cfg)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    assert cfg.storage.live_frames is not None
    tracker = wc._get_live_tracker(cfg.storage.live_frames)
    sid = tracker.create_session("daily_ops_report", "2026-11-17")
    evt = tracker.record_event(
        sid,
        phase="OA 报备填单",
        action="FILL",
        target="#oa-net-sales",
        detail="自动填写净销售额",
        current_url="http://127.0.0.1:8765/oa/submit",
        frame_bytes=b"\xff\xd8\xff\xe0FAKEJPEG",
    )
    tracker.finish_session(sid, "SUCCESS", {"oa_ticket_id": "OA-20261117-001"})

    client = TestClient(wc.app)
    r_live = client.get(f"/api/live/{sid}")
    assert r_live.status_code == 200
    sess = r_live.json()["session"]
    assert sess["status"] == "SUCCESS"
    assert len(sess["events"]) == 1
    assert evt["frame_url"] is not None

    r_frame = client.get(evt["frame_url"])
    assert r_frame.status_code == 200
    assert r_frame.content.startswith(b"\xff\xd8")
