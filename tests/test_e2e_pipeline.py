from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

from docx import Document
import openpyxl
import uvicorn

from core.config_loader import load_config
from core.pipeline import run_backfill_pipeline, run_daily_pipeline
from mock_systems.server import app as mock_app
from storage.state_db import StateDB


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_e2e_pipeline_with_mock_systems(tmp_path: Path) -> None:
    port = _find_free_port()
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)

    try:
        cfg = load_config()
        cfg.storage.root = tmp_path
        cfg.storage.db_path = tmp_path / "state.db"
        cfg.storage.auth_states = tmp_path / "auth_states"
        cfg.storage.downloads = tmp_path / "downloads"
        cfg.storage.outputs = tmp_path / "outputs"
        cfg.storage.screenshots = tmp_path / "screenshots"
        cfg.storage.dropzone = tmp_path / "dropzone"
        cfg.storage.logs = tmp_path / "logs"
        cfg.storage.backups = tmp_path / "backups"
        cfg.storage.live_frames = tmp_path / "live_frames"
        cfg.storage.ensure_all()

        base = f"http://127.0.0.1:{port}"
        cfg.connectors.mock_crm.base_url = base
        cfg.connectors.mock_ads.base_url = base
        cfg.connectors.mock_finance.base_url = base
        cfg.connectors.mock_oa.base_url = base
        cfg.browser.headless = True
        cfg.notifier.dry_run = True

        # Place an extra offline CSV file in dropzone to verify LocalDropConnector
        (cfg.storage.dropzone / "offline_cost.csv").write_text(
            "biz_date,channel,extra_cost\n2026-11-17,抖音信息流,120.0\n",
            encoding="utf-8-sig",
        )

        # Backfill 3 days ending 2026-11-17 (verifies SPA Finance + XHR + PDF + Backfill + Charts)
        outcomes = run_backfill_pipeline(
            days=3,
            end_date="2026-11-17",
            force=True,
            dry_run=True,
            cfg=cfg,
        )
        assert len(outcomes) == 3
        assert all(o.status == "SUCCESS" for o in outcomes)

        latest = outcomes[-1]
        assert latest.reconcile_diff_count >= 3
        assert latest.oa_ticket_id is not None and latest.oa_ticket_id.startswith("OA-")
        assert latest.excel_path is not None and latest.excel_path.exists()
        assert latest.word_path is not None and latest.word_path.exists()
        assert latest.notify_preview_path is not None and latest.notify_preview_path.exists()

        wb = openpyxl.load_workbook(latest.excel_path)
        assert set(wb.sheetnames) == {
            "经营总览",
            "渠道投放与ROI明细",
            "财务对账差异明细",
            "订单原始明细",
        }
        assert len(wb["经营总览"]._charts) == 2

        doc = Document(latest.word_path)
        assert len(doc.inline_shapes) >= 1

        trend = StateDB(cfg.storage.db_path).get_daily_trend(cfg.job_name, limit_days=14)
        assert len(trend) == 3

        # Second run without force should hit idempotency lock
        second = run_daily_pipeline(biz_date="2026-11-17", force=False, dry_run=True, cfg=cfg)
        assert second.status == "SKIPPED"

        # Failure drill should capture screenshot and mark FAILED
        drill = run_daily_pipeline(
            biz_date="2026-11-17",
            force=True,
            dry_run=True,
            cfg=cfg,
            simulate_failure=True,
        )
        assert drill.status == "FAILED"
        assert drill.screenshot_path is not None and drill.screenshot_path.exists()
    finally:
        server.should_exit = True
        thread.join(timeout=3)
