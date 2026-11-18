from __future__ import annotations

from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment

from processors.expense_rules import ExpenseAuditResult
from reporters.excel_reporter import (
    _ANOMALY_FILL,
    _ANOMALY_FONT,
    _BODY_FONT,
    _HEADER_FILL,
    _HEADER_FONT,
    _KPI_FILL,
    _THIN_BORDER,
    _TITLE_FONT,
    _WARN_FILL,
    _WARN_FONT,
    _autofit_columns,
)


def generate_expense_excel_report(
    audit: ExpenseAuditResult,
    output_dir: Path,
    ticket_id: str = "",
) -> Path:
    """Render a 3-sheet styled Excel reimbursement report and risk audit ledger."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"expense_report_{audit.biz_date}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "报销汇总总览"
    ws["A1"] = f"发票智能审查与自动报销台账 ({audit.biz_date})"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:F1")

    kpi_rows = [
        ("申报人", audit.kpis.get("applicant", "-"), "所属部门", audit.kpis.get("department", "-")),
        ("审验票据总数", audit.total_count, "票面总金额", round(audit.total_amount, 2)),
        ("合规可报销张数", audit.approved_count, "合规提交金额", round(audit.approved_amount, 2)),
        ("规则拦截张数", audit.flagged_count, "拦截风险金额", round(audit.flagged_amount, 2)),
        ("报销系统回执单号", ticket_id or "无合规单据未提交", "采购抬头核验基准", audit.kpis.get("company_name", "")),
    ]
    for row_idx, (k1, v1, k2, v2) in enumerate(kpi_rows, start=3):
        ws.cell(row_idx, 1, k1).fill = _KPI_FILL
        ws.cell(row_idx, 1).font = _BODY_FONT
        ws.cell(row_idx, 2, v1).font = _BODY_FONT
        ws.cell(row_idx, 3, k2).fill = _KPI_FILL
        ws.cell(row_idx, 3).font = _BODY_FONT
        ws.cell(row_idx, 4, v2).font = _BODY_FONT
        for col in range(1, 5):
            ws.cell(row_idx, col).border = _THIN_BORDER
            if isinstance(ws.cell(row_idx, col).value, float):
                ws.cell(row_idx, col).number_format = "#,##0.00"

    ws["A9"] = "审查结论摘要"
    ws["A9"].font = _HEADER_FONT
    ws["A9"].fill = _HEADER_FILL
    ws.merge_cells("A9:F9")
    ws["A10"] = audit.summary_text
    ws["A10"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A10:F14")

    # Sheet 2: 合规自动填报明细
    ws_ok = wb.create_sheet("合规自动填报明细")
    ok_headers = ["文件名", "发票号码", "开票日期", "费用类别", "销售方", "不含税金额", "税额", "价税合计", "识别来源", "置信度"]
    for idx, h in enumerate(ok_headers, start=1):
        c = ws_ok.cell(1, idx, h)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.border = _THIN_BORDER

    for r_idx, item in enumerate(audit.approved_items, start=2):
        r = item.record
        vals = [
            r.file_name,
            r.invoice_number,
            r.issue_date,
            r.category,
            r.seller_name,
            r.amount_excl_tax,
            r.tax_amount,
            r.total_amount,
            "AI大模型" if r.extract_source == "ai" else "规则解析",
            f"{r.confidence * 100:.0f}%",
        ]
        for c_idx, v in enumerate(vals, start=1):
            cell = ws_ok.cell(r_idx, c_idx, v)
            cell.font = _BODY_FONT
            cell.border = _THIN_BORDER
            if c_idx in {6, 7, 8}:
                cell.number_format = "#,##0.00"

    # Sheet 3: 风控拦截清单
    ws_risk = wb.create_sheet("风控拦截清单")
    risk_headers = ["文件名", "发票号码", "开票日期", "类别", "购买方", "价税合计", "类目上限", "拦截原因"]
    for idx, h in enumerate(risk_headers, start=1):
        c = ws_risk.cell(1, idx, h)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.border = _THIN_BORDER

    for r_idx, item in enumerate(audit.flagged_items, start=2):
        r = item.record
        vals = [
            r.file_name,
            r.invoice_number or "-",
            r.issue_date or "-",
            r.category,
            r.buyer_name or "-",
            r.total_amount,
            item.limit_amount,
            "；".join(item.risk_reasons),
        ]
        for c_idx, v in enumerate(vals, start=1):
            cell = ws_risk.cell(r_idx, c_idx, v)
            cell.fill = _ANOMALY_FILL if c_idx == 8 else _WARN_FILL
            cell.font = _ANOMALY_FONT if c_idx == 8 else _WARN_FONT
            cell.border = _THIN_BORDER
            if c_idx in {6, 7}:
                cell.number_format = "#,##0.00"

    for sheet in (ws, ws_ok, ws_risk):
        _autofit_columns(sheet)

    wb.save(out_path)
    return out_path
