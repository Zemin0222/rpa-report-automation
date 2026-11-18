from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage
import json
from pathlib import Path
import sqlite3

from connectors.imap_mail_connector import parse_raw_email_bytes
from connectors.local_drop_connector import LocalDropConnector
from connectors.registry import apply_column_mapping, build_connector
from core.config_loader import NotifierConfig, SourceSpec, StoragePaths, load_config
from core.doctor import run_doctor_checks
from core.pipeline import run_daily_pipeline
from notifiers.webhook import (
    compute_dingtalk_sign,
    compute_feishu_sign,
    send_failure_alert,
)
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
    )
    cfg.storage.ensure_all()
    return cfg


def test_webhook_signing_and_failure_alert(tmp_path: Path, monkeypatch) -> None:
    feishu_sig = compute_feishu_sign("sec_feishu", 1700000000)
    ding_sig = compute_dingtalk_sign("sec_ding", 1700000000000)
    assert len(feishu_sig) > 20
    assert len(ding_sig) > 20

    monkeypatch.setenv("FEISHU_WEBHOOK_SECRET", "sec_feishu")
    shot = tmp_path / "fail_shot.png"
    shot.write_bytes(b"png")

    preview = send_failure_alert(
        NotifierConfig(dry_run=True, channel="feishu"),
        job_name="daily_ops_report",
        biz_date="2026-11-17",
        error_message="Selector timeout on #export-btn",
        screenshot_path=shot,
        output_dir=tmp_path,
        force_dry_run=True,
    )
    assert preview.name == "alert_preview_2026-11-17.json"
    assert preview.exists()
    data = json.loads(preview.read_text(encoding="utf-8"))
    assert data["msg_type"] == "interactive"
    assert data["card"]["header"]["template"] == "red"
    assert "sign" in data and "timestamp" in data


def test_running_lock_expiry_and_backup(tmp_path: Path) -> None:
    cfg = _isolated_cfg(tmp_path)
    db = StateDB(cfg.storage.db_path)

    run_id = db.start_run("daily_ops_report", "2026-11-17")
    active = db.get_running_lock("daily_ops_report", "2026-11-17", stale_minutes=30)
    assert active is not None and active["id"] == run_id

    # Pipeline should block concurrent execution while active lock exists
    blocked = run_daily_pipeline(biz_date="2026-11-17", force=True, cfg=cfg)
    assert blocked.status == "LOCKED"

    # Backdate started_at by 2 hours to simulate a crashed stale RUNNING lock
    stale_ts = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    with sqlite3.connect(cfg.storage.db_path) as conn:
        conn.execute("UPDATE job_runs SET started_at = ? WHERE id = ?", (stale_ts, run_id))

    expired = db.get_running_lock("daily_ops_report", "2026-11-17", stale_minutes=30)
    assert expired is None

    # Online hot backup & pruning
    assert cfg.storage.backups is not None
    b1 = db.backup_database(cfg.storage.backups, keep_count=2)
    b2 = db.backup_database(cfg.storage.backups, keep_count=2)
    b3 = db.backup_database(cfg.storage.backups, keep_count=2)
    assert b3.exists()
    remaining = list(cfg.storage.backups.glob("state_backup_*.db"))
    assert len(remaining) == 2
    assert b1 not in remaining and b2.exists() and b3.exists()


def test_imap_mime_parsing_and_deduplication(tmp_path: Path) -> None:
    cfg = _isolated_cfg(tmp_path)
    db = StateDB(cfg.storage.db_path)

    msg = EmailMessage()
    msg["Subject"] = "2026-11-17 每日渠道消耗表"
    msg["From"] = "finance@example.com"
    msg["To"] = "ops@example.com"
    msg.set_content("请查收今日附件。")
    msg.add_attachment(
        "biz_date,channel,extra_cost\n2026-11-17,抖音信息流,88.5\n".encode("utf-8-sig"),
        maintype="text",
        subtype="csv",
        filename="channel_cost.csv",
    )
    raw_bytes = msg.as_bytes()

    save_dir = tmp_path / "mail_dl"
    first = parse_raw_email_bytes(
        raw_bytes,
        biz_date="2026-11-17",
        save_dir=save_dir,
        subject_keyword="每日渠道消耗",
        db=db,
        deduplicate=True,
    )
    assert len(first) == 1 and first[0].name == "channel_cost.csv"

    # Re-parsing the exact same attachment should be skipped by SHA256 dedup
    second = parse_raw_email_bytes(
        raw_bytes,
        biz_date="2026-11-17",
        save_dir=save_dir,
        subject_keyword="每日渠道消耗",
        db=db,
        deduplicate=True,
    )
    assert second == []


def test_dropzone_mapping_dedup_and_archive(tmp_path: Path) -> None:
    cfg = _isolated_cfg(tmp_path)
    db = StateDB(cfg.storage.db_path)

    src_csv = cfg.storage.dropzone / "raw_spend.csv"
    src_csv.write_text(
        "日期,推广渠道,线下费用\n2026-11-17,抖音,150.0\n",
        encoding="utf-8-sig",
    )

    spec = SourceSpec(
        id="local_drop",
        connector="local_drop",
        role="extra_cost",
        column_mapping={"日期": "biz_date", "推广渠道": "channel", "线下费用": "extra_cost"},
        archive_after_read=True,
        deduplicate=True,
    )
    connector = build_connector(spec, cfg, db=db)
    assert isinstance(connector, LocalDropConnector)

    res = connector.fetch("2026-11-17")
    assert res.row_count == 1
    assert list(res.dataframe["channel"]) == ["抖音"]
    assert float(res.dataframe["extra_cost"].iloc[0]) == 150.0
    assert not src_csv.exists()
    archived = cfg.storage.dropzone / "archive" / "2026-11-17" / "raw_spend.csv"
    assert archived.exists()

    # Dropping an identical file again should be deduplicated
    dup_csv = cfg.storage.dropzone / "raw_spend_copy.csv"
    dup_csv.write_text(
        "日期,推广渠道,线下费用\n2026-11-17,抖音,150.0\n",
        encoding="utf-8-sig",
    )
    res_dup = connector.fetch("2026-11-17")
    assert res_dup.row_count == 0


def test_doctor_and_registry_helpers(tmp_path: Path) -> None:
    cfg = _isolated_cfg(tmp_path)
    checks = run_doctor_checks(cfg)
    assert len(checks) >= 5
    assert any("SQLite" in c["检查项"] and c["状态"] == "OK" for c in checks)

    mapped = apply_column_mapping(
        LocalDropConnector(cfg).fetch("2026-11-17").dataframe,
        {"extra_cost": "cost_yuan"},
    )
    assert "cost_yuan" in mapped.columns
    job_def = cfg.get_job("daily_ops_report")
    assert len(job_def.sources) >= 4
