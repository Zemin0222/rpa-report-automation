from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from docx import Document
from docx.shared import Inches, Pt
from PIL import Image, ImageDraw, ImageFont

from processors.etl import EtlResult


def _load_pil_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render_chart_pillow(etl: EtlResult, trend_rows: list[dict[str, Any]] | None) -> io.BytesIO:
    """Dependency-free high-DPI PNG chart renderer using Pillow + Noto CJK / Microsoft YaHei."""
    width, height = 1480, 560
    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    font_title = _load_pil_font(22)
    font_lbl = _load_pil_font(15)
    font_sm = _load_pil_font(13)

    # Left panel: Channel Net Sales vs Ad Spend
    lx0, ly0, lx1, ly1 = 70, 70, 700, 460
    draw.text((lx0, 26), f"各渠道净销售额 vs 广告消耗 ({etl.biz_date})", fill="#0F172A", font=font_title)
    draw.line((lx0, ly1, lx1, ly1), fill="#CBD5E1", width=2)
    draw.line((lx0, ly0, lx0, ly1), fill="#CBD5E1", width=2)

    df_ch = etl.channel_df
    max_val = max(
        1.0,
        float(df_ch["net_sales"].max() if not df_ch.empty else 0.0),
        float(df_ch["ad_spend"].max() if not df_ch.empty else 0.0),
    )
    n_ch = max(1, len(df_ch))
    slot_w = (lx1 - lx0 - 20) / n_ch
    bar_w = max(12, min(26, int(slot_w * 0.32)))

    for idx, rec in df_ch.iterrows():
        cx = lx0 + 20 + idx * slot_w + slot_w * 0.45
        h_sales = int((float(rec["net_sales"]) / max_val) * (ly1 - ly0 - 20))
        h_spend = int((float(rec["ad_spend"]) / max_val) * (ly1 - ly0 - 20))
        draw.rectangle([cx - bar_w - 2, ly1 - h_sales, cx - 2, ly1], fill="#1D4ED8")
        draw.rectangle([cx + 2, ly1 - h_spend, cx + bar_w + 2, ly1], fill="#94A3B8")
        ch_name = str(rec["channel"])[:5]
        draw.text((cx - bar_w, ly1 + 10), ch_name, fill="#334155", font=font_sm)

    draw.rectangle([lx1 - 190, 32, lx1 - 174, 46], fill="#1D4ED8")
    draw.text((lx1 - 168, 29), "净销售额", fill="#334155", font=font_lbl)
    draw.rectangle([lx1 - 95, 32, lx1 - 79, 46], fill="#94A3B8")
    draw.text((lx1 - 73, 29), "广告消耗", fill="#334155", font=font_lbl)

    # Right panel: Multi-day Net Sales trend (or Channel ROI bars)
    rx0, ry0, rx1, ry1 = 790, 70, 1420, 460
    draw.line((rx0, ry1, rx1, ry1), fill="#CBD5E1", width=2)
    draw.line((rx0, ry0, rx0, ry1), fill="#CBD5E1", width=2)

    trend = list(trend_rows or [])
    if len(trend) >= 2:
        draw.text((rx0, 26), "多日净销售额趋势 (元)", fill="#0F172A", font=font_title)
        t_max = max(1.0, max(float(r["total_net_sales"]) for r in trend))
        pts: list[tuple[int, int]] = []
        step_x = (rx1 - rx0 - 40) / max(1, len(trend) - 1)
        for idx, rec in enumerate(trend):
            px = int(rx0 + 20 + idx * step_x)
            py = int(ry1 - (float(rec["total_net_sales"]) / t_max) * (ry1 - ry0 - 30))
            pts.append((px, py))
            draw.text((px - 18, ry1 + 10), str(rec["biz_date"])[5:], fill="#334155", font=font_sm)
        if len(pts) >= 2:
            draw.line(pts, fill="#0F766E", width=4)
        for px, py in pts:
            draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill="#0F766E")
    else:
        draw.text((rx0, 26), "各渠道 ROI 表现", fill="#0F172A", font=font_title)
        max_roi = max(2.5, float(df_ch["roi"].max() if not df_ch.empty else 2.5))
        for idx, rec in df_ch.iterrows():
            cx = rx0 + 20 + idx * slot_w + slot_w * 0.45
            h_roi = int((float(rec["roi"]) / max_roi) * (ry1 - ry0 - 20))
            draw.rectangle([cx - 14, ry1 - h_roi, cx + 14, ry1], fill="#0F766E")
            draw.text((cx - 18, ry1 + 10), str(rec["channel"])[:5], fill="#334155", font=font_sm)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _render_chart_png(etl: EtlResult, trend_rows: list[dict[str, Any]] | None) -> io.BytesIO | None:
    if etl.channel_df.empty:
        return None
    try:
        import matplotlib
        import matplotlib.font_manager as fm

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        preferred = [
            "Noto Sans CJK SC",
            "Noto Sans CJK JP",
            "Microsoft YaHei",
            "SimHei",
            "WenQuanYi Zen Hei",
            "DejaVu Sans",
        ]
        installed = {f.name for f in fm.fontManager.ttflist}
        chosen = [name for name in preferred if name in installed]
        if chosen:
            plt.rcParams["font.sans-serif"] = chosen + ["DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.5, 4.0), dpi=160)
        fig.patch.set_facecolor("#FFFFFF")

        df_ch = etl.channel_df.copy()
        channels = df_ch["channel"].tolist()
        x = range(len(channels))
        width = 0.36
        ax1.bar([i - width / 2 for i in x], df_ch["net_sales"], width=width, label="净销售额", color="#1D4ED8")
        ax1.bar([i + width / 2 for i in x], df_ch["ad_spend"], width=width, label="广告消耗", color="#94A3B8")
        ax1.set_xticks(list(x))
        ax1.set_xticklabels(channels, rotation=18, ha="right", fontsize=8.5)
        ax1.set_title(f"各渠道营收与消耗 ({etl.biz_date})", fontsize=10.5, fontweight="bold")
        ax1.set_ylabel("金额 (元)", fontsize=9)
        ax1.legend(fontsize=8)
        ax1.grid(axis="y", linestyle="--", alpha=0.35)
        ax1.spines["top"].set_visible(False)
        ax1.spines["right"].set_visible(False)

        trend = list(trend_rows or [])
        if len(trend) >= 2:
            dates = [r["biz_date"][5:] for r in trend]
            sales = [r["total_net_sales"] for r in trend]
            rois = [r["overall_roi"] for r in trend]
            ax2.plot(dates, sales, marker="o", color="#0F766E", linewidth=2, label="净销售额")
            ax2.set_title("多日净销售额与 ROI 走势", fontsize=10.5, fontweight="bold")
            ax2.set_ylabel("净销售额 (元)", fontsize=9, color="#0F766E")
            ax2.tick_params(axis="x", labelsize=8)
            ax2.grid(axis="y", linestyle="--", alpha=0.35)
            ax2.spines["top"].set_visible(False)
            ax2_twin = ax2.twinx()
            ax2_twin.plot(dates, rois, marker="s", linestyle="--", color="#B91C1C", linewidth=1.6, label="整体 ROI")
            ax2_twin.set_ylabel("ROI", fontsize=9, color="#B91C1C")
            ax2_twin.spines["top"].set_visible(False)
        else:
            ax2.bar(channels, df_ch["roi"], color="#0F766E", width=0.45)
            ax2.axhline(2.0, color="#B91C1C", linestyle="--", linewidth=1.2, label="ROI 警戒线 (2.0)")
            ax2.set_xticks(list(x))
            ax2.set_xticklabels(channels, rotation=18, ha="right", fontsize=8.5)
            ax2.set_title("各渠道 ROI 表现", fontsize=10.5, fontweight="bold")
            ax2.set_ylabel("ROI", fontsize=9)
            ax2.legend(fontsize=8)
            ax2.grid(axis="y", linestyle="--", alpha=0.35)
            ax2.spines["top"].set_visible(False)
            ax2.spines["right"].set_visible(False)

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160)
        plt.close(fig)
        buf.seek(0)
        return buf
    except ImportError:
        return _render_chart_pillow(etl, trend_rows)


def generate_word_report(
    etl: EtlResult,
    summary_text: str,
    output_dir: Path,
    trend_rows: list[dict[str, Any]] | None = None,
) -> Path:
    """Render a formatted Word operations brief with embedded trend charts and reconciliation table."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"weekly_report_{etl.biz_date}.docx"

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)

    doc.add_heading(f"跨系统经营数据自动简报 ({etl.biz_date})", level=1)

    doc.add_heading("一、核心经营摘要", level=2)
    for line in summary_text.splitlines():
        if line.strip():
            doc.add_paragraph(line.strip())

    chart_buf = _render_chart_png(etl, trend_rows)
    if chart_buf is not None:
        doc.add_heading("二、经营可视化趋势", level=2)
        doc.add_picture(chart_buf, width=Inches(6.3))

    doc.add_heading("三、核心指标汇总", level=2)
    k = etl.kpis
    rec_sum = etl.reconcile_summary or {}
    kpi_items = [
        ("总订单数", f"{k['total_orders']} 笔"),
        ("总 GMV", f"¥{k['total_gmv']:,.2f}"),
        ("净销售额", f"¥{k['total_net_sales']:,.2f}"),
        ("广告总消耗", f"¥{k['total_ad_spend']:,.2f}"),
        ("整体 ROI", f"{k['overall_roi']:.2f}"),
        ("异常渠道数", f"{k['anomaly_count']} 个"),
        (
            "对账差异",
            f"{rec_sum.get('discrepancy_count', 0)} 笔 (¥{rec_sum.get('total_diff_amount', 0.0):,.2f})",
        ),
    ]
    kpi_table = doc.add_table(rows=1, cols=2)
    kpi_table.style = "Table Grid"
    kpi_table.rows[0].cells[0].text = "指标项"
    kpi_table.rows[0].cells[1].text = "数值"
    for label, val in kpi_items:
        row = kpi_table.add_row().cells
        row[0].text = label
        row[1].text = val

    doc.add_heading("四、各渠道投放与销售明细", level=2)
    if not etl.channel_df.empty:
        cols = ["渠道", "订单数", "净销售额", "广告消耗", "ROI", "状态"]
        ch_table = doc.add_table(rows=1, cols=len(cols))
        ch_table.style = "Table Grid"
        for idx, title in enumerate(cols):
            ch_table.rows[0].cells[idx].text = title
        for rec in etl.channel_df.to_dict(orient="records"):
            row = ch_table.add_row().cells
            row[0].text = str(rec["channel"])
            row[1].text = str(rec["order_count"])
            row[2].text = f"¥{float(rec['net_sales']):,.2f}"
            row[3].text = f"¥{float(rec['ad_spend']):,.2f}"
            row[4].text = f"{float(rec['roi']):.2f}" if rec.get("has_ad_spend") else "-"
            row[5].text = str(rec["anomaly_reason"])

    doc.add_heading("五、CRM 与财务对账差异明细", level=2)
    if not etl.reconcile_df.empty:
        r_cols = ["订单号", "渠道", "CRM净额", "结算金额", "差异额", "差异类型"]
        r_table = doc.add_table(rows=1, cols=len(r_cols))
        r_table.style = "Table Grid"
        for idx, title in enumerate(r_cols):
            r_table.rows[0].cells[idx].text = title
        for rec in etl.reconcile_df.to_dict(orient="records"):
            row = r_table.add_row().cells
            row[0].text = str(rec["order_id"])
            row[1].text = str(rec["channel"])
            row[2].text = f"¥{float(rec['crm_net_amount']):,.2f}"
            row[3].text = f"¥{float(rec['settled_amount']):,.2f}"
            row[4].text = f"¥{float(rec['diff_amount']):,.2f}"
            row[5].text = str(rec["diff_type"])
    else:
        doc.add_paragraph("当日 CRM 订单与财务结算完全一致，无差异单。")

    doc.save(out_path)
    return out_path
