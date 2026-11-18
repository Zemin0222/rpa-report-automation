from __future__ import annotations

from pathlib import Path
from storage.state_db import StateDB


def test_state_db_idempotency_and_steps(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")

    assert db.get_successful_run("daily_ops_report", "2026-11-17") is None

    run_id = db.start_run("daily_ops_report", "2026-11-17")
    db.log_step(run_id, "collect_crm", "DONE", "rows=40")
    db.finish_success(
        run_id=run_id,
        duration_sec=1.25,
        row_count=40,
        anomaly_count=1,
        excel_path="outputs/daily.xlsx",
        word_path="outputs/weekly.docx",
        notify_preview_path="outputs/notify.json",
        summary_text="ok",
        kpi_payload={"channels": [{"channel": "抖音信息流", "net_sales": 1200.0}]},
    )

    hit = db.get_successful_run("daily_ops_report", "2026-11-17")
    assert hit is not None
    assert hit["id"] == run_id

    steps = db.list_steps_for_run(run_id)
    assert len(steps) == 1
    assert steps[0]["step_name"] == "collect_crm"

    prev = db.get_previous_kpi_map("daily_ops_report", "2026-11-18")
    assert prev == {"抖音信息流": 1200.0}
