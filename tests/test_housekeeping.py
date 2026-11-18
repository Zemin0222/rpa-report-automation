from __future__ import annotations

import os
import time
from pathlib import Path

from core.config_loader import RetentionConfig, StoragePaths
from core.housekeeping import cleanup_storage, get_storage_usage_mb


def _make_storage(tmp_path: Path) -> StoragePaths:
    sp = StoragePaths(
        root=tmp_path,
        db_path=tmp_path / "state.db",
        auth_states=tmp_path / "auth_states",
        downloads=tmp_path / "downloads",
        outputs=tmp_path / "outputs",
        screenshots=tmp_path / "screenshots",
        dropzone=tmp_path / "dropzone",
        logs=tmp_path / "logs",
    )
    sp.ensure_all()
    return sp


def test_cleanup_storage_dry_run_and_apply(tmp_path: Path) -> None:
    sp = _make_storage(tmp_path)
    retention = RetentionConfig(downloads_days=7, screenshots_days=7, outputs_days=30, logs_days=14)

    old_dir = sp.downloads / "2026-10-01"
    old_dir.mkdir(parents=True, exist_ok=True)
    old_csv = old_dir / "old.csv"
    old_csv.write_text("a,b\n1,2\n", encoding="utf-8")

    fresh_csv = sp.downloads / "fresh.csv"
    fresh_csv.write_text("a,b\n3,4\n", encoding="utf-8")

    gitkeep = sp.downloads / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    old_shot = sp.screenshots / "fail_old.png"
    old_shot.write_bytes(b"PNGDATA")

    auth_file = sp.auth_states / "mock_crm.json"
    auth_file.write_text("{}", encoding="utf-8")

    past_ts = time.time() - (20 * 86400)
    os.utime(old_csv, (past_ts, past_ts))
    os.utime(gitkeep, (past_ts, past_ts))
    os.utime(old_shot, (past_ts, past_ts))
    os.utime(auth_file, (past_ts, past_ts))

    preview = cleanup_storage(sp, retention, dry_run=True)
    assert preview["total_files_removed"] == 2
    assert old_csv.exists()
    assert old_shot.exists()

    applied = cleanup_storage(sp, retention, dry_run=False)
    assert applied["total_files_removed"] == 2
    assert not old_csv.exists()
    assert not old_shot.exists()
    assert not old_dir.exists()  # empty date subdir removed
    assert fresh_csv.exists()
    assert gitkeep.exists()
    assert auth_file.exists()

    usage = get_storage_usage_mb(sp)
    assert set(usage.keys()) == {"downloads", "outputs", "screenshots", "auth_states", "logs"}
