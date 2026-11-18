from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from core.browser import BrowserManager
from core.config_loader import AppConfig, JobDefinition
from core.live_observer import LiveRunTracker
from storage.state_db import StateDB


@dataclass
class RunContext:
    """Mutable runtime context shared across collect -> process -> deliver stages."""

    run_id: int
    cfg: AppConfig
    job_def: JobDefinition
    db: StateDB
    biz_date: str
    dry_run: bool | None = None
    simulate_failure: bool = False
    browser_mgr: BrowserManager | None = None
    observer: LiveRunTracker | None = None
    session_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def log_step(
        self,
        step_name: str,
        status: str = "DONE",
        detail: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        self.db.log_step(
            self.run_id,
            step_name,
            status,
            detail=detail,
            duration_ms=duration_ms,
        )

    def record_live(
        self,
        phase: str,
        action: str,
        target: str,
        detail: str,
    ) -> None:
        if self.observer and self.session_id:
            self.observer.record_event(
                self.session_id,
                phase,
                action,
                target,
                detail,
            )


@dataclass
class CollectedData:
    """Output of Workflow.collect() passed into Workflow.process()."""

    frames_by_role: dict[str, list[pd.DataFrame]] = field(default_factory=dict)
    row_count: int = 0
    payload: Any = None


@dataclass
class ProcessResult:
    """Output of Workflow.process() passed into Workflow.deliver() and kpi_view()."""

    row_count: int = 0
    anomaly_count: int = 0
    reconcile_diff_count: int = 0
    summary_text: str = ""
    kpis: dict[str, Any] = field(default_factory=dict)
    payload: Any = None


@dataclass
class DeliveryResult:
    """Artifacts and receipts produced by Workflow.deliver()."""

    oa_ticket_id: str | None = None
    excel_path: Path | None = None
    word_path: Path | None = None
    notify_preview_path: Path | None = None
    payload: Any = None


@dataclass(frozen=True)
class WorkflowDescriptor:
    """Lightweight workflow metadata exposed to CLI and web workbench."""

    key: str
    title: str
    description: str
    view_type: str


@runtime_checkable
class Workflow(Protocol):
    """Plugin contract implemented by every business RPA workflow scenario."""

    key: str
    title: str
    description: str
    view_type: str

    def needs_browser(
        self,
        cfg: AppConfig,
        job_def: JobDefinition,
        simulate_failure: bool = False,
    ) -> bool:
        """Whether this run requires launching a Playwright BrowserManager context."""
        ...

    def run_failure_drill(self, ctx: RunContext) -> None:
        """Optional chaos drill triggered when simulate_failure=True."""
        ...

    def collect(self, ctx: RunContext) -> CollectedData:
        """Collect raw files, tables, or API payloads for target business date."""
        ...

    def process(self, ctx: RunContext, data: CollectedData) -> ProcessResult:
        """Normalize, validate, reconcile, or enrich collected data."""
        ...

    def deliver(self, ctx: RunContext, result: ProcessResult) -> DeliveryResult:
        """Export deliverables, submit target systems, and send notifications."""
        ...

    def kpi_view(self, result: ProcessResult) -> dict[str, Any]:
        """Serialize structured metrics & items stored in StateDB for UI rendering."""
        ...
