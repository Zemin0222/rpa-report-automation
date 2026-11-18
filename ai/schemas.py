from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class InvoiceExtractionSchema(BaseModel):
    """Structured VAT invoice schema validated on LLM output."""

    invoice_code: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    item_name: str = ""
    category: Literal["交通", "住宿", "餐饮", "办公", "通信"] = "办公"
    amount_excl_tax: float = 0.0
    tax_amount: float = 0.0
    total_amount: float = 0.0


class ChannelAnomalyExplanation(BaseModel):
    """Structured root-cause analysis & concrete remediation steps for an abnormal channel."""

    channel: str
    root_cause: str
    impact: str
    recommended_actions: list[str] = Field(default_factory=list)
    urgency: Literal["高", "中", "低"] = "中"


class ReconcileAttribution(BaseModel):
    """Likely causes and verification checklist for a CRM-vs-Finance settlement discrepancy."""

    order_id: str
    likely_causes: list[str] = Field(default_factory=list)
    check_steps: list[str] = Field(default_factory=list)
    priority: Literal["P1", "P2", "P3"] = "P2"


class DailyExecutiveAdvice(BaseModel):
    """Executive highlights and concrete optimization suggestions from daily operational metrics."""

    highlights: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
