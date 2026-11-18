from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class StateDB:
    """SQLite state store with WAL concurrency, idempotency locks, and step auditing."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    biz_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_sec REAL,
                    row_count INTEGER DEFAULT 0,
                    anomaly_count INTEGER DEFAULT 0,
                    excel_path TEXT,
                    word_path TEXT,
                    notify_preview_path TEXT,
                    screenshot_path TEXT,
                    error_message TEXT,
                    summary_text TEXT,
                    kpi_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_job_runs_lookup
                    ON job_runs (job_name, biz_date, status);

                CREATE TABLE IF NOT EXISTS step_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    duration_ms REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES job_runs(id)
                );

                CREATE TABLE IF NOT EXISTS processed_files (
                    file_hash TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    biz_date TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_cache (
                    cache_key TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    model TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    tokens_used INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usage_date TEXT NOT NULL,
                    task TEXT NOT NULL,
                    source TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_usage_date
                    ON ai_usage (usage_date, source);
                """
            )
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(step_logs)").fetchall()
            }
            if "duration_ms" not in cols:
                conn.execute("ALTER TABLE step_logs ADD COLUMN duration_ms REAL")

    def get_successful_run(self, job_name: str, biz_date: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM job_runs
                WHERE job_name = ? AND biz_date = ? AND status = 'SUCCESS'
                ORDER BY id DESC LIMIT 1
                """,
                (job_name, biz_date),
            ).fetchone()
            return dict(row) if row else None

    def get_running_lock(
        self,
        job_name: str,
        biz_date: str,
        stale_minutes: int = 30,
    ) -> dict[str, Any] | None:
        """Return active RUNNING record unless stale past stale_minutes (in which case expire it)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM job_runs
                WHERE job_name = ? AND biz_date = ? AND status = 'RUNNING'
                ORDER BY id DESC LIMIT 1
                """,
                (job_name, biz_date),
            ).fetchone()
            if not row:
                return None

            rec = dict(row)
            started_dt = datetime.fromisoformat(rec["started_at"])
            age_seconds = (datetime.now() - started_dt).total_seconds()
            if age_seconds <= stale_minutes * 60:
                return rec

            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE job_runs
                SET status = 'FAILED',
                    finished_at = ?,
                    error_message = 'Stale RUNNING lock timed out'
                WHERE id = ?
                """,
                (now, rec["id"]),
            )
            return None

    def is_file_processed(self, file_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_files WHERE file_hash = ? LIMIT 1",
                (file_hash,),
            ).fetchone()
            return row is not None

    def mark_file_processed(
        self,
        file_hash: str,
        source_name: str,
        file_name: str,
        biz_date: str,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_files (file_hash, source_name, file_name, biz_date, processed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file_hash, source_name, file_name, biz_date, now),
            )

    def backup_database(self, backup_dir: Path, keep_count: int = 7) -> Path:
        """Create an online consistent SQLite backup and prune backups older than keep_count."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest_path = backup_dir / f"state_backup_{ts}.db"
        src_conn = self._connect()
        dst_conn = sqlite3.connect(dest_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        backups = sorted(backup_dir.glob("state_backup_*.db"), reverse=True)
        for old_file in backups[max(1, keep_count) :]:
            try:
                old_file.unlink()
            except OSError:
                pass
        return dest_path

    def get_previous_kpi_map(self, job_name: str, before_biz_date: str) -> dict[str, float]:
        """Return prior day channel -> net_sales map from the most recent successful run."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT kpi_json FROM job_runs
                WHERE job_name = ? AND biz_date < ? AND status = 'SUCCESS' AND kpi_json IS NOT NULL
                ORDER BY biz_date DESC, id DESC LIMIT 1
                """,
                (job_name, before_biz_date),
            ).fetchone()
            if not row or not row["kpi_json"]:
                return {}
            try:
                payload = json.loads(row["kpi_json"])
                return {
                    item["channel"]: float(item["net_sales"])
                    for item in payload.get("channels", [])
                    if "channel" in item and "net_sales" in item
                }
            except Exception:
                return {}

    def start_run(self, job_name: str, biz_date: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO job_runs (job_name, biz_date, status, started_at)
                VALUES (?, ?, 'RUNNING', ?)
                """,
                (job_name, biz_date, now),
            )
            return int(cur.lastrowid)

    def log_step(
        self,
        run_id: int,
        step_name: str,
        status: str,
        detail: str = "",
        duration_ms: float | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO step_logs (run_id, step_name, status, detail, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_name,
                    status,
                    detail,
                    round(duration_ms, 1) if duration_ms is not None else None,
                    now,
                ),
            )

    def finish_success(
        self,
        run_id: int,
        duration_sec: float,
        row_count: int,
        anomaly_count: int,
        excel_path: str | None,
        word_path: str | None,
        notify_preview_path: str | None,
        summary_text: str,
        kpi_payload: dict[str, Any],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_runs
                SET status = 'SUCCESS',
                    finished_at = ?,
                    duration_sec = ?,
                    row_count = ?,
                    anomaly_count = ?,
                    excel_path = ?,
                    word_path = ?,
                    notify_preview_path = ?,
                    summary_text = ?,
                    kpi_json = ?
                WHERE id = ?
                """,
                (
                    now,
                    round(duration_sec, 2),
                    row_count,
                    anomaly_count,
                    excel_path,
                    word_path,
                    notify_preview_path,
                    summary_text,
                    json.dumps(kpi_payload, ensure_ascii=False),
                    run_id,
                ),
            )

    def finish_failed(
        self,
        run_id: int,
        duration_sec: float,
        error_message: str,
        screenshot_path: str | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_runs
                SET status = 'FAILED',
                    finished_at = ?,
                    duration_sec = ?,
                    error_message = ?,
                    screenshot_path = ?
                WHERE id = ?
                """,
                (now, round(duration_sec, 2), error_message, screenshot_path, run_id),
            )

    def list_recent_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_steps_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM step_logs WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_daily_trend(self, job_name: str, limit_days: int = 14) -> list[dict[str, Any]]:
        """Return chronological daily KPI summaries (latest successful run per biz_date)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT j.biz_date, j.kpi_json
                FROM job_runs j
                INNER JOIN (
                    SELECT biz_date, MAX(id) AS max_id
                    FROM job_runs
                    WHERE job_name = ? AND status = 'SUCCESS' AND kpi_json IS NOT NULL
                    GROUP BY biz_date
                ) latest ON j.id = latest.max_id
                ORDER BY j.biz_date DESC
                LIMIT ?
                """,
                (job_name, limit_days),
            ).fetchall()

        trend: list[dict[str, Any]] = []
        for r in reversed(rows):
            try:
                kpi = json.loads(r["kpi_json"])
            except Exception:
                continue
            rec_sum = kpi.get("reconcile_summary") or {}
            trend.append(
                {
                    "biz_date": r["biz_date"],
                    "total_orders": int(kpi.get("total_orders", 0)),
                    "total_gmv": float(kpi.get("total_gmv", 0.0)),
                    "total_net_sales": float(kpi.get("total_net_sales", 0.0)),
                    "total_ad_spend": float(kpi.get("total_ad_spend", 0.0)),
                    "overall_roi": float(kpi.get("overall_roi", 0.0)),
                    "anomaly_count": int(kpi.get("anomaly_count", 0)),
                    "reconcile_diff_count": int(rec_sum.get("discrepancy_count", 0)),
                    "reconcile_diff_amount": float(rec_sum.get("total_diff_amount", 0.0)),
                }
            )
        return trend

    def get_ai_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cache_key, task, model, response_json, tokens_used, created_at FROM ai_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            return dict(row) if row else None

    def put_ai_cache(
        self,
        cache_key: str,
        task: str,
        model: str,
        response_json: str,
        tokens_used: int = 0,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ai_cache (cache_key, task, model, response_json, tokens_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cache_key, task, model, response_json, int(tokens_used), now),
            )

    def record_ai_usage(
        self,
        usage_date: str,
        task: str,
        source: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_usage (
                    usage_date, task, source, model,
                    prompt_tokens, completion_tokens, total_tokens,
                    latency_ms, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_date,
                    task,
                    source,
                    model,
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    round(latency_ms, 1),
                    now,
                ),
            )

    def get_ai_daily_stats(self, usage_date: str | None = None) -> dict[str, Any]:
        target_date = usage_date or datetime.now().date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, COUNT(*) AS cnt, COALESCE(SUM(total_tokens), 0) AS tok
                FROM ai_usage
                WHERE usage_date = ?
                GROUP BY source
                """,
                (target_date,),
            ).fetchall()
            recent = conn.execute(
                """
                SELECT task, source, model, total_tokens, latency_ms, created_at
                FROM ai_usage
                ORDER BY id DESC LIMIT 6
                """
            ).fetchall()
            cache_entries = conn.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0]

        by_src = {r["source"]: {"cnt": int(r["cnt"]), "tok": int(r["tok"])} for r in rows}
        ai_calls = by_src.get("ai", {}).get("cnt", 0)
        tokens_today = by_src.get("ai", {}).get("tok", 0)
        cache_hits = by_src.get("cache", {}).get("cnt", 0)
        rule_fallbacks = by_src.get("rule", {}).get("cnt", 0)
        llm_denom = ai_calls + cache_hits
        hit_rate = round((cache_hits / llm_denom) * 100, 1) if llm_denom > 0 else 0.0
        return {
            "usage_date": target_date,
            "calls_today": ai_calls,
            "tokens_today": tokens_today,
            "cache_hits_today": cache_hits,
            "rule_fallbacks_today": rule_fallbacks,
            "cache_hit_rate_pct": hit_rate,
            "cache_entries_total": int(cache_entries),
            "recent_events": [dict(r) for r in recent],
        }

    def reset_demo_records(self, preserve_ai_cache: bool = True) -> None:
        """Clear transactional run/telemetry tables for nightly public-demo reset."""
        with self._connect() as conn:
            conn.execute("DELETE FROM step_logs")
            conn.execute("DELETE FROM job_runs")
            conn.execute("DELETE FROM processed_files")
            conn.execute("DELETE FROM ai_usage")
            if not preserve_ai_cache:
                conn.execute("DELETE FROM ai_cache")
            conn.commit()
