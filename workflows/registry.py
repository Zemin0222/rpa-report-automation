from __future__ import annotations

from workflows.base import Workflow, WorkflowDescriptor
from workflows.daily_ops import DailyOpsWorkflow
from workflows.expense_reimburse import ExpenseReimburseWorkflow

_WORKFLOW_REGISTRY: dict[str, Workflow] = {}


def register_workflow(workflow: Workflow) -> None:
    """Register a workflow plugin by its unique key."""
    _WORKFLOW_REGISTRY[workflow.key] = workflow


def get_workflow(key: str) -> Workflow:
    """Retrieve a registered workflow plugin by key, raising ValueError if unknown."""
    wf = _WORKFLOW_REGISTRY.get(key)
    if wf is None:
        known = ", ".join(sorted(_WORKFLOW_REGISTRY)) or "(none)"
        raise ValueError(f"Unknown workflow '{key}'. Registered workflows: {known}")
    return wf


def list_workflows() -> list[WorkflowDescriptor]:
    """Return metadata descriptors for all registered workflows in registration order."""
    return [
        WorkflowDescriptor(
            key=wf.key,
            title=wf.title,
            description=wf.description,
            view_type=wf.view_type,
        )
        for wf in _WORKFLOW_REGISTRY.values()
    ]


register_workflow(DailyOpsWorkflow())
register_workflow(ExpenseReimburseWorkflow())
