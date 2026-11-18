from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any, Callable, Literal
import uuid

from core.logger import get_logger

logger = get_logger("rpa.queue")

TicketStatus = Literal["QUEUED", "RUNNING", "DONE", "FAILED"]


class QueueFullError(RuntimeError):
    """Raised when the single-worker RPA queue exceeds configured capacity."""


@dataclass
class QueueTicket:
    """Tracking metadata for an enqueued or active RPA execution."""

    ticket_id: str
    kind: str
    job_name: str
    biz_date: str
    session_id: str | None = None
    status: TicketStatus = "QUEUED"
    enqueued_at: float = field(default_factory=time.perf_counter)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str = ""
    done_event: threading.Event = field(default_factory=threading.Event)


class SingleWorkerQueue:
    """In-process single-slot FIFO queue ensuring only one Playwright/RPA run executes at once."""

    def __init__(self, max_queue_size: int = 10, default_estimate_sec: float = 20.0) -> None:
        self.max_queue_size = max(1, int(max_queue_size))
        self._avg_duration_sec = float(default_estimate_sec)
        self._lock = threading.Lock()
        self._work_q: queue.Queue[tuple[str, Callable[[], Any]]] = queue.Queue()
        self._waiting_ids: list[str] = []
        self._running_id: str | None = None
        self._tickets: dict[str, QueueTicket] = {}
        self._history_order: list[str] = []
        self._worker = threading.Thread(target=self._worker_loop, name="rpa-single-worker", daemon=True)
        self._worker.start()

    def _prune_tickets_locked(self) -> None:
        while len(self._history_order) > 60:
            oldest = self._history_order.pop(0)
            if oldest != self._running_id and oldest not in self._waiting_ids:
                self._tickets.pop(oldest, None)

    def _worker_loop(self) -> None:
        while True:
            ticket_id, fn = self._work_q.get()
            with self._lock:
                if ticket_id in self._waiting_ids:
                    self._waiting_ids.remove(ticket_id)
                self._running_id = ticket_id
                ticket = self._tickets.get(ticket_id)
                if ticket is not None:
                    ticket.status = "RUNNING"
                    ticket.started_at = time.perf_counter()

            started = time.perf_counter()
            result: Any = None
            err_msg = ""
            final_status: TicketStatus = "DONE"
            try:
                result = fn()
            except Exception as exc:  # pragma: no cover - captured via ticket state
                final_status = "FAILED"
                err_msg = str(exc)
                logger.exception("Queued RPA task %s failed: %s", ticket_id, exc)
            finally:
                elapsed = max(0.2, time.perf_counter() - started)
                with self._lock:
                    if ticket is not None:
                        ticket.status = final_status
                        ticket.result = result
                        ticket.error = err_msg
                        ticket.finished_at = time.perf_counter()
                        ticket.done_event.set()
                    self._running_id = None
                    # Smooth rolling duration estimate bounded cleanly
                    self._avg_duration_sec = round(
                        min(60.0, max(2.0, 0.65 * self._avg_duration_sec + 0.35 * elapsed)),
                        1,
                    )
                self._work_q.task_done()

    def enqueue(
        self,
        fn: Callable[[], Any],
        *,
        kind: str = "visual",
        job_name: str = "daily_ops_report",
        biz_date: str = "",
        session_id: str | None = None,
    ) -> tuple[QueueTicket, int, int, int]:
        """Enqueue a callable; returns (ticket, queue_position, ahead_count, estimated_wait_sec)."""
        tid = uuid.uuid4().hex[:12]
        ticket = QueueTicket(
            ticket_id=tid,
            kind=kind,
            job_name=job_name,
            biz_date=biz_date,
            session_id=session_id,
        )
        with self._lock:
            if len(self._waiting_ids) >= self.max_queue_size:
                raise QueueFullError(
                    f"当前演示队列已有 {len(self._waiting_ids)} 个任务排队，已达上限，请稍后再试。"
                )
            ahead_count = (1 if self._running_id is not None else 0) + len(self._waiting_ids)
            est_wait = 0 if ahead_count == 0 else max(1, int(round(ahead_count * self._avg_duration_sec)))
            self._tickets[tid] = ticket
            self._history_order.append(tid)
            self._waiting_ids.append(tid)
            self._prune_tickets_locked()
            self._work_q.put((tid, fn))
        return ticket, ahead_count, ahead_count, est_wait

    def get_status(self, ticket_id: str | None = None) -> dict[str, Any]:
        """Return live worker occupancy plus optional per-ticket position & wait estimate."""
        with self._lock:
            running_ticket = self._tickets.get(self._running_id) if self._running_id else None
            running_payload: dict[str, Any] | None = None
            if running_ticket is not None:
                elapsed = (
                    round(time.perf_counter() - running_ticket.started_at, 1)
                    if running_ticket.started_at
                    else 0.0
                )
                running_payload = {
                    "ticket_id": running_ticket.ticket_id,
                    "kind": running_ticket.kind,
                    "job_name": running_ticket.job_name,
                    "biz_date": running_ticket.biz_date,
                    "session_id": running_ticket.session_id,
                    "elapsed_sec": elapsed,
                }

            ticket_payload: dict[str, Any] | None = None
            if ticket_id:
                t = self._tickets.get(ticket_id)
                if t is not None:
                    if t.ticket_id == self._running_id or t.status in {"DONE", "FAILED"}:
                        ahead = 0
                        est = 0
                    elif t.ticket_id in self._waiting_ids:
                        idx = self._waiting_ids.index(t.ticket_id)
                        ahead = (1 if self._running_id is not None else 0) + idx
                        est = max(1, int(round(ahead * self._avg_duration_sec)))
                    else:
                        ahead = 0
                        est = 0
                    ticket_payload = {
                        "ticket_id": t.ticket_id,
                        "status": t.status,
                        "position": ahead,
                        "ahead_count": ahead,
                        "estimated_wait_sec": est,
                        "session_id": t.session_id,
                        "error": t.error,
                    }

            return {
                "busy": self._running_id is not None or len(self._waiting_ids) > 0,
                "running": running_payload,
                "queued_count": len(self._waiting_ids),
                "max_queue_size": self.max_queue_size,
                "avg_duration_sec": self._avg_duration_sec,
                "ticket": ticket_payload,
            }
