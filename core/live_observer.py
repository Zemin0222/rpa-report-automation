from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import threading
import time
from typing import Any
import uuid


@dataclass
class LiveSession:
    session_id: str
    job_name: str
    biz_date: str
    started_at: float
    status: str = "RUNNING"  # RUNNING | SUCCESS | FAILED | SKIPPED | LOCKED
    current_url: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] | None = None


class LiveRunTracker:
    """Thread-safe live RPA execution recorder capturing action logs and keyframe screenshots."""

    def __init__(self, frames_root: Path, max_sessions: int = 5) -> None:
        self.frames_root = frames_root
        self.frames_root.mkdir(parents=True, exist_ok=True)
        self.max_sessions = max_sessions
        self._lock = threading.Lock()
        self._sessions: dict[str, LiveSession] = {}
        self._latest_id: str | None = None

    def create_session(self, job_name: str, biz_date: str) -> str:
        sid = uuid.uuid4().hex[:12]
        sess_dir = self.frames_root / sid
        sess_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._sessions[sid] = LiveSession(
                session_id=sid,
                job_name=job_name,
                biz_date=biz_date,
                started_at=time.perf_counter(),
            )
            self._latest_id = sid
            self._prune_old_sessions_locked()
        return sid

    def _prune_old_sessions_locked(self) -> None:
        if len(self._sessions) > self.max_sessions:
            oldest_keys = list(self._sessions.keys())[: -self.max_sessions]
            for k in oldest_keys:
                self._sessions.pop(k, None)
        dirs = sorted(
            (p for p in self.frames_root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_dir in dirs[self.max_sessions :]:
            shutil.rmtree(old_dir, ignore_errors=True)

    def record_event(
        self,
        session_id: str,
        phase: str,
        action: str,
        target: str,
        detail: str,
        current_url: str = "",
        frame_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return {}
            seq = len(sess.events) + 1
            elapsed = round(time.perf_counter() - sess.started_at, 2)
            if current_url:
                sess.current_url = current_url

            frame_name: str | None = None
            if frame_bytes:
                frame_name = f"frame_{seq:03d}.jpg"
                (self.frames_root / session_id / frame_name).write_bytes(frame_bytes)

            evt = {
                "seq": seq,
                "elapsed_sec": elapsed,
                "phase": phase,
                "action": action,
                "target": target,
                "detail": detail,
                "url": sess.current_url,
                "frame_name": frame_name,
                "frame_url": f"/api/live/frame/{session_id}/{frame_name}" if frame_name else None,
            }
            sess.events.append(evt)
            return evt

    def finish_session(self, session_id: str, status: str, outcome: dict[str, Any] | None = None) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess.status = status
                sess.outcome = outcome

    def get_session_dict(self, session_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            target_id = session_id or self._latest_id
            if not target_id:
                return None
            sess = self._sessions.get(target_id)
            if sess is None:
                return None
            latest_frame = next(
                (e["frame_url"] for e in reversed(sess.events) if e.get("frame_url")),
                None,
            )
            return {
                "session_id": sess.session_id,
                "job_name": sess.job_name,
                "biz_date": sess.biz_date,
                "status": sess.status,
                "elapsed_sec": round(time.perf_counter() - sess.started_at, 2)
                if sess.status == "RUNNING"
                else (sess.outcome or {}).get("duration_sec", round(time.perf_counter() - sess.started_at, 2)),
                "current_url": sess.current_url,
                "current_frame_url": latest_frame,
                "events": list(sess.events),
                "outcome": sess.outcome,
            }
