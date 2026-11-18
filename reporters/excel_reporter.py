from __future__ import annotations

from pathlib import Path
from typing import Any
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from processors.etl import EtlResult

_HEADER_FILL = PatternFill("solid", fgColor="1E293B")
_HEADER_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
_BODY_FONT = Font(name="Microsoft YaHei", size=10, color="1E293B")
_TITLE_FONT = Font(name="Microsoft YaHei", size=14, bold=True, color="0F172A")
_ANOMALY_FILL = PatternFill("solid", fgColor="FEE2E2")
_ANOMALY_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="991B1B")
_WARN_FILL = PatternFill("solid", fgColor="FEF3C7")
_WARN_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="92400E")
_KPI_FILL = PatternFill("solid", fgColor="F1F5F9")
_THIN_BORDER = Border(
    left=Side(style="thin", color="CBD5E1"),
    right=Side(style="thin", color="CBD5E1"),
    top=Side(style="thin", color="CBD5E1"),
    bottom=Side(style="thin", color="CBD5E1"),
)


def _autofit_columns(ws, min_width: int = 10, max_width: int = 38) -> None:
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val = "" if cell.value is None else str(cell.value)
            display_len = sum(2 if ord(ch) > 127 else 1 for ch in val)
            if display_len > max_len:
                max_len = display_len
        ws.column_dimensions[col_letter].width = max(min_width, min(max_width, max_len + 4))


def generate_excel_report(
    etl: EtlResult,
    summary_text: str,
    output_dir: Path,
    trend_rows: list[dict[str, Any]] | None = None,
) -> Path:
    """Render a multi-sheet styled Excel daily report with charts and reconciliation sheet."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"daily_report_{etl.biz_date}.xlsx"

    wb = Workbook()

    # Sheet 1: 经营总览
    ws_overview = wb.active
    ws_overview.title = "经营总览"
    ws_overview.views.sheetView[0].showGridLines = True

    ws_overview["A1"] = f"跨系统经营日报总览 ({etl.biz_date})"
    ws_overview["A1"].font = _TITLE_FONT
    ws_overview.merge_cells("A1:F1")
    ws_overview.row_dimensions[1].height = 28

    k = etl.kpis
    rec_sum = etl.reconcile_summary or {}
    kpi_pairs = [
        ("总订单数", k["total_orders"], "#,##0"),
        ("总 GMV (元)", k["total_gmv"], "¥#,##0.00"),
        ("净销售额 (元)", k["total_net_sales"], "¥#,##0.00"),
        ("广告总消耗 (元)", k["total_ad_spend"], "¥#,##0.00"),
        ("整体 ROI", k["overall_roi"], "0.00"),
        ("对账差异单数", rec_sum.get("discrepancy_count", 0), "#,##0"),
    ]
    for idx, (label, val, num_fmt) in enumerate(kpi_pairs, start=1):
        lbl_cell = ws_overview.cell(row=3, column=idx, value=label)
        lbl_cell.font = _HEADER_FONT
        lbl_cell.fill = _HEADER_FILL
        lbl_cell.alignment = Alignment(horizontal="center", vertical="center")
        lbl_cell.border = _THIN_BORDER

        val_cell = ws_overview.cell(row=4, column=idx, value=val)
        val_cell.font = Font(name="Microsoft YaHei", size=12, bold=True, color="0F172A")
        val_cell.fill = _KPI_FILL
        val_cell.number_format = num_fmt
        val_cell.alignment = Alignment(horizontal="center", vertical="center")
        val_cell.border = _THIN_BORDER

    ws_overview.row_dimensions[3].height = 22
    ws_overview.row_dimensions[4].height = 26

    ws_overview["A6"] = "日报摘要与异常说明"
    ws_overview["A6"].font = Font(name="Microsoft YaHei", size=11, bold=True, color="0F172A")
    summary_lines = summary_text.splitlines()
    for offset, line in enumerate(summary_lines, start=7):
        cell = ws_overview.cell(row=offset, column=1, value=line)
        cell.font = _BODY_FONT
        ws_overview.merge_cells(start_row=offset, start_column=1, end_row=offset, end_column=6)

    # Trend table on Sheet 1 for native LineChart
    trend_start_row = 7 + len(summary_lines) + 2
    trend_data = list(trend_rows or [])
    if not trend_data or trend_data[-1].get("biz_date") != etl.biz_date:
        trend_data = [r for r in trend_data if r.get("biz_date") != etl.biz_date] + [
            {
                "biz_date": etl.biz_date,
                "total_net_sales": k["total_net_sales"],
                "total_ad_spend": k["total_ad_spend"],
                "overall_roi": k["overall_roi"],
                "total_orders": k["total_orders"],
            }
        ]

    ws_overview.cell(row=trend_start_row, column=1, value="多日经营趋势明细").font = Font(
        name="Microsoft YaHei", size=11, bold=True, color="0F172A"
    )
    t_hdr_row = trend_start_row + 1
    t_headers = [
        ("日期", "biz_date", None),
        ("订单数", "total_orders", "#,##0"),
        ("净销售额 (元)", "total_net_sales", "¥#,##0.00"),
        ("广告消耗 (元)", "total_ad_spend", "¥#,##0.00"),
        ("整体 ROI", "overall_roi", "0.00"),
    ]
    for col_idx, (title, _, _) in enumerate(t_headers, start=1):
        c = ws_overview.cell(row=t_hdr_row, column=col_idx, value=title)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _THIN_BORDER

    for r_off, t_rec in enumerate(trend_data, start=1):
        r_num = t_hdr_row + r_off
        for col_idx, (_, key, num_fmt) in enumerate(t_headers, start=1):
            c = ws_overview.cell(row=r_num, column=col_idx, value=t_rec.get(key))
            c.font = _BODY_FONT
            c.border = _THIN_BORDER
            if num_fmt:
                c.number_format = num_fmt

    _autofit_columns(ws_overview, min_width=15, max_width=28)

    # Sheet 2: 渠道投放与ROI明细
    ws_ch = wb.create_sheet("渠道投放与ROI明细")
    ws_ch.freeze_panes = "A2"
    ch_headers = [
        ("日期", "biz_date", None),
        ("渠道", "channel", None),
        ("订单数", "order_count", "#,##0"),
        ("GMV (元)", "gmv", "¥#,##0.00"),
        ("退款额 (元)", "refund_amount", "¥#,##0.00"),
        ("净销售额 (元)", "net_sales", "¥#,##0.00"),
        ("退款率 (%)", "refund_rate_pct", "0.00"),
        ("客单价 (元)", "aov", "¥#,##0.00"),
        ("曝光量", "impressions", "#,##0"),
        ("点击量", "clicks", "#,##0"),
        ("点击率 (%)", "ctr_pct", "0.00"),
        ("广告消耗 (元)", "ad_spend", "¥#,##0.00"),
        ("ROI", "roi", "0.00"),
        ("获客成本 CAC", "cac", "¥#,##0.00"),
        ("异常状态", "anomaly_reason", None),
    ]
    for col_idx, (title, _, _) in enumerate(ch_headers, start=1):
        c = ws_ch.cell(row=1, column=col_idx, value=title)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _THIN_BORDER
    ws_ch.row_dimensions[1].height = 24

    ch_row_count = 0
    if not etl.channel_df.empty:
        records = etl.channel_df.to_dict(orient="records")
        ch_row_count = len(records)
        for row_idx, rec in enumerate(records, start=2):
            is_bad = bool(rec.get("is_anomaly"))
            for col_idx, (_, key, num_fmt) in enumerate(ch_headers, start=1):
                val = rec.get(key)
                cell = ws_ch.cell(row=row_idx, column=col_idx, value=val)
                cell.font = _ANOMALY_FONT if is_bad else _BODY_FONT
                if is_bad:
                    cell.fill = _ANOMALY_FILL
                cell.border = _THIN_BORDER
                if num_fmt:
                    cell.number_format = num_fmt
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
    _autofit_columns(ws_ch)

    # Embed native Excel charts on Sheet 1 (placed neatly to the right at H3 / H20)
    if ch_row_count > 0:
        bar_chart = BarChart()
        bar_chart.type = "col"
        bar_chart.style = 10
        bar_chart.title = f"各渠道净销售额 vs 广告消耗 ({etl.biz_date})"
        bar_chart.y_axis.title = "金额 (元)"
        sales_ref = Reference(ws_ch, min_col=6, min_row=1, max_row=1 + ch_row_count)
        spend_ref = Reference(ws_ch, min_col=12, min_row=1, max_row=1 + ch_row_count)
        cats_ref = Reference(ws_ch, min_col=2, min_row=2, max_row=1 + ch_row_count)
        bar_chart.add_data(sales_ref, titles_from_data=True)
        bar_chart.add_data(spend_ref, titles_from_data=True)
        bar_chart.set_categories(cats_ref)
        bar_chart.width = 16
        bar_chart.height = 9.5
        ws_overview.add_chart(bar_chart, "H3")

    if len(trend_data) >= 1:
        line_chart = LineChart()
        line_chart.style = 10
        line_chart.title = "近期净销售额与广告消耗走势"
        line_chart.y_axis.title = "金额 (元)"
        t_max_row = t_hdr_row + len(trend_data)
        t_data_ref = Reference(ws_overview, min_col=3, max_col=4, min_row=t_hdr_row, max_row=t_max_row)
        t_cats_ref = Reference(ws_overview, min_col=1, min_row=t_hdr_row + 1, max_row=t_max_row)
        line_chart.add_data(t_data_ref, titles_from_data=True)
        line_chart.set_categories(t_cats_ref)
        line_chart.width = 16
        line_chart.height = 9.5
        ws_overview.add_chart(line_chart, "H22")

    # Sheet 3: 财务对账差异明细
    ws_rec = wb.create_sheet("财务对账差异明细")
    ws_rec.freeze_panes = "A2"
    rec_cols = [
        ("订单号", "order_id", None),
        ("渠道", "channel", None),
        ("CRM净额 (元)", "crm_net_amount", "¥#,##0.00"),
        ("财务结算金额 (元)", "settled_amount", "¥#,##0.00"),
        ("差异金额 (元)", "diff_amount", "¥#,##0.00"),
        ("差异类型", "diff_type", None),
        ("财务状态", "finance_status", None),
        ("排查建议", "action_advice", None),
    ]
    for col_idx, (title, _, _) in enumerate(rec_cols, start=1):
        c = ws_rec.cell(row=1, column=col_idx, value=title)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _THIN_BORDER
    ws_rec.row_dimensions[1].height = 24

    if not etl.reconcile_df.empty:
        for row_idx, rec in enumerate(etl.reconcile_df.to_dict(orient="records"), start=2):
            for col_idx, (_, key, num_fmt) in enumerate(rec_cols, start=1):
                cell = ws_rec.cell(row=row_idx, column=col_idx, value=rec.get(key))
                cell.font = _WARN_FONT
                cell.fill = _WARN_FILL
                cell.border = _THIN_BORDER
                if num_fmt:
                    cell.number_format = num_fmt
    else:
        ws_rec.cell(row=2, column=1, value="当日 CRM 与财务结算记录完全匹配，无差异订单。").font = _BODY_FONT
    _autofit_columns(ws_rec)

    # Sheet 4: 订单原始明细
    ws_ord = wb.create_sheet("订单原始明细")
    ws_ord.freeze_panes = "A2"
    ord_cols = [
        ("订单号", "order_id"),
        ("日期", "biz_date"),
        ("渠道", "channel"),
        ("客户层级", "customer_tier"),
        ("订单金额", "order_amount"),
        ("退款金额", "refund_amount"),
        ("状态", "order_status"),
    ]
    for col_idx, (title, _) in enumerate(ord_cols, start=1):
        c = ws_ord.cell(row=1, column=col_idx, value=title)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _THIN_BORDER
    if not etl.orders_df.empty:
        for row_idx, rec in enumerate(etl.orders_df.to_dict(orient="records"), start=2):
            for col_idx, (_, key) in enumerate(ord_cols, start=1):
                cell = ws_ord.cell(row=row_idx, column=col_idx, value=rec.get(key))
                cell.font = _BODY_FONT
                cell.border = _THIN_BORDER
                if key in {"order_amount", "refund_amount"}:
                    cell.number_format = "¥#,##0.00"
    _autofit_columns(ws_ord)

    wb.save(out_path)
    return out_path
