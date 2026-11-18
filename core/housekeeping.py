from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from core.config_loader import RetentionConfig, StoragePaths
from core.logger import get_logger

logger = get_logger("rpa.housekeeping")


def _purge_old_files(
    target_dir: Path,
    keep_days: int,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Delete files older than keep_days inside target_dir, preserving .gitkeep."""
    if not target_dir.exists() or keep_days < 0:
        return 0, 0

    cutoff = time.time() - (keep_days * 86400)
    removed_files = 0
    reclaimed_bytes = 0

    for item in sorted(target_dir.rglob("*")):
        if not item.is_file() or item.name == ".gitkeep":
            continue
        try:
            stat = item.stat()
            if stat.st_mtime < cutoff:
                reclaimed_bytes += stat.st_size
                removed_files += 1
                if not dry_run:
                    item.unlink()
        except OSError as exc:
            logger.warning("Could not inspect/delete %s: %s", item, exc)

    if not dry_run:
        # Clean up empty date subdirectories left under downloads/
        for sub in sorted(target_dir.rglob("*"), reverse=True):
            if sub.is_dir():
                try:
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    pass

    return removed_files, reclaimed_bytes


def cleanup_storage(
    storage: StoragePaths,
    retention: RetentionConfig,
    dry_run: bool = False,
) -> dict[str, int]:
    """Enforce retention rules on downloads, screenshots, outputs, and logs (never touches auth_states or DB)."""
    targets: Iterable[tuple[str, Path, int]] = (
        ("downloads", storage.downloads, retention.downloads_days),
        ("screenshots", storage.screenshots, retention.screenshots_days),
        ("outputs", storage.outputs, retention.outputs_days),
        ("logs", storage.logs, retention.logs_days),
    )
    summary: dict[str, int] = {"total_files_removed": 0, "total_bytes_reclaimed": 0}
    for key, path, days in targets:
        cnt, size_bytes = _purge_old_files(path, days, dry_run=dry_run)
        summary[f"{key}_removed"] = cnt
        summary["total_files_removed"] += cnt
        summary["total_bytes_reclaimed"] += size_bytes

    if summary["total_files_removed"] > 0:
        logger.info(
            "Housekeeping (%s): removed=%d files, reclaimed=%d bytes",
            "dry-run" if dry_run else "applied",
            summary["total_files_removed"],
            summary["total_bytes_reclaimed"],
        )
    return summary


def get_storage_usage_mb(storage: StoragePaths) -> dict[str, float]:
    """Compute disk usage (MB) per storage bucket for the dashboard."""
    buckets = {
        "downloads": storage.downloads,
        "outputs": storage.outputs,
        "screenshots": storage.screenshots,
        "auth_states": storage.auth_states,
        "logs": storage.logs,
    }
    usage: dict[str, float] = {}
    for name, folder in buckets.items():
        total = 0
        if folder.exists():
            for f in folder.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        usage[name] = round(total / (1024 * 1024), 3)
    return usage
