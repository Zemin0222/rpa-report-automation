from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.web_console as wc
from core.config_loader import JobDefinition, StoragePaths, load_config
from core.pipeline import run_daily_pipeline
from core.runner import run_workflow
from storage.state_db import StateDB
from workflows.base import (
    CollectedData,
    DeliveryResult,
    ProcessResult,
    RunContext,
    Workflow,
)
from workflows.registry import _WORKFLOW_REGISTRY, get_workflow, list_workflows, register_workflow


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
        live_frames=tmp_path / "live_frames",
    )
    cfg.storage.ensure_all()
    cfg.notifier.dry_run = True
    return cfg


def test_registry_lists_daily_ops_and_rejects_unknown() -> None:
    descriptors = list_workflows()
    keys = {d.key for d in descriptors}
    assert "daily_ops_report" in keys

    wf = get_workflow("daily_ops_report")
    assert isinstance(wf, Workflow)
    assert wf.view_type == "daily_ops"
    assert wf.title

    with pytest.raises(ValueError, match="Unknown workflow"):
        get_workflow("nonexistent_workflow_xyz")


def test_custom_workflow_lifecycle_idempotency_and_failure(tmp_path: Path) -> None:
    stages: list[str] = []

    class StubExpenseWorkflow:
        key = "stub_expense_test"
        title = "测试报销插件"
        description = "验证可插拔 Workflow 执行生命周期"
        view_type = "expense"

        def needs_browser(self, cfg, job_def, simulate_failure: bool = False) -> bool:
            return False

        def run_failure_drill(self, ctx: RunContext) -> None:
            raise RuntimeError("stub drill boom")

        def collect(self, ctx: RunContext) -> CollectedData:
            stages.append("collect")
            ctx.log_step("stub_collect", "DONE", "files=3")
            return CollectedData(row_count=3, payload={"items": ["inv1", "inv2", "inv3"]})

        def process(self, ctx: RunContext, data: CollectedData) -> ProcessResult:
            stages.append("process")
            ctx.log_step("stub_validate", "DONE", f"items={data.row_count}")
            return ProcessResult(
                row_count=data.row_count,
                anomaly_count=1,
                reconcile_diff_count=0,
                summary_text="已审核 3 张发票，1 张超标拦截",
                kpis={
                    "cards": [{"label": "待报销总额", "value": "¥880.00", "sub": "3 张发票"}],
                    "total_orders": 3,
                },
            )

        def deliver(self, ctx: RunContext, result: ProcessResult) -> DeliveryResult:
            stages.append("deliver")
            ctx.log_step("stub_submit", "DONE", "ticket=EXP-TEST-001")
            return DeliveryResult(oa_ticket_id="EXP-TEST-001")

        def kpi_view(self, result: ProcessResult) -> dict[str, Any]:
            stages.append("kpi_view")
            return result.kpis

    wf = StubExpenseWorkflow()
    register_workflow(wf)
    try:
        cfg = _isolated_cfg(tmp_path)
        cfg.jobs.append(
            JobDefinition(
                job_name="stub_expense_job",
                workflow=wf.key,
                title="自定义测试报销任务",
                description="仅单元测试使用",
                sources=[],
            )
        )

        # Dispatch via run_daily_pipeline facade (proving config workflow routing works)
        out = run_daily_pipeline(biz_date="2026-11-18", force=True, cfg=cfg, job_name="stub_expense_job")
        assert out.status == "SUCCESS"
        assert stages == ["collect", "process", "deliver", "kpi_view"]
        assert out.row_count == 3
        assert out.anomaly_count == 1
        assert out.oa_ticket_id == "EXP-TEST-001"
        assert out.kpis == {
            "cards": [{"label": "待报销总额", "value": "¥880.00", "sub": "3 张发票"}],
            "total_orders": 3,
        }

        db = StateDB(cfg.storage.db_path)
        saved = db.get_successful_run("stub_expense_job", "2026-11-18")
        assert saved is not None
        assert json.loads(saved["kpi_json"])["total_orders"] == 3
        step_names = [s["step_name"] for s in db.list_steps_for_run(out.run_id)]
        assert step_names == ["stub_collect", "stub_validate", "stub_submit"]

        # Idempotency hit on rerun without force
        skipped = run_workflow(wf, biz_date="2026-11-18", force=False, cfg=cfg, job_name="stub_expense_job")
        assert skipped.status == "SKIPPED"
        assert len(stages) == 4  # workflow stages were NOT re-invoked

        # Failure handling records FAILED and generates alert preview
        failed = run_workflow(
            wf,
            biz_date="2026-11-18",
            force=True,
            dry_run=True,
            simulate_failure=True,
            cfg=cfg,
            job_name="stub_expense_job",
        )
        assert failed.status == "FAILED"
        assert "stub drill boom" in failed.error_message
        assert (cfg.storage.outputs / "alert_preview_2026-11-18.json").exists()
    finally:
        _WORKFLOW_REGISTRY.pop("stub_expense_test", None)


def test_web_console_exposes_workflow_metadata(tmp_path: Path, monkeypatch) -> None:
    cfg = _isolated_cfg(tmp_path)
    monkeypatch.setattr(wc, "load_config", lambda: cfg)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    client = TestClient(wc.app)
    resp = client.get("/api/state")
    assert resp.status_code == 200
    payload = resp.json()
    assert "workflows" in payload and "jobs_meta" in payload and "active_workflow" in payload
    assert any(w["key"] == "daily_ops_report" and w["view_type"] == "daily_ops" for w in payload["workflows"])
    assert payload["active_workflow"]["workflow"] == "daily_ops_report"
    assert payload["active_workflow"]["title"]
