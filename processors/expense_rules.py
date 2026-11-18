from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.config_loader import ExpenseConfig
from processors.invoice_extractor import InvoiceRecord


class ExpenseAuditItem(BaseModel):
    """Single audited invoice with rule compliance verdict and risk reasons."""

    record: InvoiceRecord
    status: Literal["合规待提交", "规则拦截"]
    risk_reasons: list[str] = Field(default_factory=list)
    limit_amount: float = 0.0


@dataclass
class ExpenseAuditResult:
    biz_date: str
    items: list[ExpenseAuditItem]
    approved_items: list[ExpenseAuditItem]
    flagged_items: list[ExpenseAuditItem]
    total_count: int
    approved_count: int
    flagged_count: int
    total_amount: float
    approved_amount: float
    flagged_amount: float
    summary_text: str
    kpis: dict[str, Any]


def audit_invoices(
    records: list[InvoiceRecord],
    biz_date: str,
    cfg: ExpenseConfig,
    historical_invoice_numbers: set[str] | None = None,
) -> ExpenseAuditResult:
    """Validate extracted invoices for duplicates, buyer title/tax ID, category limits, and expiry."""
    ref_day = datetime.strptime(biz_date, "%Y-%m-%d").date()
    seen_numbers: set[str] = set(historical_invoice_numbers or set())
    seen_hashes: set[str] = set()

    audited: list[ExpenseAuditItem] = []
    for rec in records:
        risks: list[str] = []

        # 1. Required fields completeness
        if not rec.invoice_number or rec.total_amount <= 0:
            risks.append("关键票面字段缺失（发票号码或金额未识别，需人工复核）")

        # 2. Duplicate invoice detection (in-batch + historical)
        if rec.invoice_number and rec.invoice_number in seen_numbers:
            risks.append(f"疑似重复报销（发票号 {rec.invoice_number} 已存在）")
        elif rec.file_hash and rec.file_hash in seen_hashes:
            risks.append("疑似重复上传（文件内容哈希完全一致）")

        if rec.invoice_number:
            seen_numbers.add(rec.invoice_number)
        if rec.file_hash:
            seen_hashes.add(rec.file_hash)

        # 3. Buyer company name & tax ID verification
        if rec.buyer_name.strip() != cfg.company_name.strip():
            risks.append(
                f"购买方抬头不符（票面为「{rec.buyer_name or '未识别'}」，应为「{cfg.company_name}」）"
            )
        if cfg.company_tax_id and rec.buyer_tax_id.strip() != cfg.company_tax_id.strip():
            risks.append(
                f"纳税人识别号不符（票面为「{rec.buyer_tax_id or '缺失'}」）"
            )

        # 4. Category single-invoice spending cap
        limit = float(cfg.category_limits.get(rec.category, 500.0))
        if rec.total_amount > limit:
            risks.append(
                f"{rec.category}费用超标（¥{rec.total_amount:.2f} > 单笔上限 ¥{limit:.2f}）"
            )

        # 5. Invoice issue date age check
        if rec.issue_date:
            try:
                issue_day = datetime.strptime(rec.issue_date, "%Y-%m-%d").date()
                age_days = (ref_day - issue_day).days
                if age_days > cfg.max_invoice_age_days:
                    risks.append(
                        f"发票超期不可报销（开票于 {rec.issue_date}，已超 {cfg.max_invoice_age_days} 天期限）"
                    )
                elif age_days < -1:
                    risks.append(f"开票日期异常（{rec.issue_date} 晚于申报日期）")
            except ValueError:
                risks.append(f"开票日期格式无法识别（{rec.issue_date}）")
        else:
            risks.append("缺少开票日期")

        status: Literal["合规待提交", "规则拦截"] = "规则拦截" if risks else "合规待提交"
        audited.append(
            ExpenseAuditItem(
                record=rec,
                status=status,
                risk_reasons=risks,
                limit_amount=limit,
            )
        )

    approved = [a for a in audited if a.status == "合规待提交"]
    flagged = [a for a in audited if a.status == "规则拦截"]
    total_amt = round(sum(a.record.total_amount for a in audited), 2)
    approved_amt = round(sum(a.record.total_amount for a in approved), 2)
    flagged_amt = round(sum(a.record.total_amount for a in flagged), 2)
    avg_conf = (
        round(sum(a.record.confidence for a in audited) / len(audited) * 100, 1)
        if audited
        else 0.0
    )
    categories = sorted({a.record.category for a in approved})

    lines = [
        f"【{biz_date} 发票智能审查与自动报销摘要】",
        (
            f"共扫描 {len(audited)} 张票据（总额 ¥{total_amt:,.2f}）："
            f"合规通过 {len(approved)} 张（¥{approved_amt:,.2f}，已自动填报提交），"
            f"规则拦截 {len(flagged)} 张（¥{flagged_amt:,.2f}，已阻断入账）。"
        ),
    ]
    if approved:
        lines.append(
            "合规入账明细："
            + "；".join(
                f"{a.record.category}({a.record.seller_name[:8]}) ¥{a.record.total_amount:.2f}"
                for a in approved
            )
        )
    if flagged:
        lines.append("风控拦截详情：")
        for f_item in flagged:
            lines.append(
                f"- {f_item.record.file_name} (¥{f_item.record.total_amount:.2f}): "
                + "；".join(f_item.risk_reasons)
            )
    summary_text = "\n".join(lines)

    serialized_items: list[dict[str, Any]] = []
    for item in audited:
        r = item.record
        serialized_items.append(
            {
                "file_name": r.file_name,
                "invoice_code": r.invoice_code,
                "invoice_number": r.invoice_number or "-",
                "issue_date": r.issue_date or "-",
                "buyer_name": r.buyer_name or "-",
                "buyer_tax_id": r.buyer_tax_id or "-",
                "seller_name": r.seller_name or "-",
                "item_name": r.item_name,
                "category": r.category,
                "amount_excl_tax": r.amount_excl_tax,
                "tax_amount": r.tax_amount,
                "total_amount": r.total_amount,
                "limit_amount": item.limit_amount,
                "extract_source": r.extract_source,
                "confidence": r.confidence,
                "thumbnail_url": r.thumbnail_url,
                "status": item.status,
                "risk_reasons": item.risk_reasons,
            }
        )

    cards = [
        {
            "label": f"本次审验票据 ({biz_date})",
            "value": f"{len(audited)} 张",
            "sub": f"票面总额 ¥{total_amt:,.2f}",
            "tone": "",
        },
        {
            "label": "合规可自动报销",
            "value": f"¥{approved_amt:,.2f}",
            "sub": f"{len(approved)} 张通过（覆盖 {'/'.join(categories) or '无'}）",
            "tone": "ok" if approved else "warn",
        },
        {
            "label": "规则拦截风险票据",
            "value": f"{len(flagged)} 张",
            "sub": f"涉及金额 ¥{flagged_amt:,.2f}",
            "tone": "warn" if flagged else "ok",
        },
        {
            "label": "识别方式与置信度",
            "value": f"{avg_conf:.1f}%",
            "sub": (
                "AI+规则协同"
                if any(a.record.extract_source in {"ai", "cache"} for a in audited)
                else "PDF 规则精准解析"
            ),
            "tone": "",
        },
        {
            "label": "报销系统回执状态",
            "value": "待提交" if approved else "无合规票据",
            "sub": f"申报人 {cfg.applicant_name}",
            "tone": "",
        },
    ]

    kpis: dict[str, Any] = {
        "total_orders": len(audited),
        "total_gmv": total_amt,
        "total_net_sales": approved_amt,
        "total_ad_spend": flagged_amt,
        "overall_roi": round(avg_conf / 100.0, 2),
        "anomaly_count": len(flagged),
        "cards": cards,
        "invoices": serialized_items,
        "expense_ticket_id": "",
        "applicant": cfg.applicant_name,
        "department": cfg.department,
        "company_name": cfg.company_name,
    }

    return ExpenseAuditResult(
        biz_date=biz_date,
        items=audited,
        approved_items=approved,
        flagged_items=flagged,
        total_count=len(audited),
        approved_count=len(approved),
        flagged_count=len(flagged),
        total_amount=total_amt,
        approved_amount=approved_amt,
        flagged_amount=flagged_amt,
        summary_text=summary_text,
        kpis=kpis,
    )
