from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pymupdf


@dataclass(frozen=True)
class SampleInvoiceSpec:
    """Definition used to synthesize a realistic Chinese VAT e-invoice PDF."""

    filename: str
    invoice_code: str
    invoice_number: str
    issue_date: str
    buyer_name: str
    buyer_tax_id: str
    seller_name: str
    item_name: str
    category: str
    amount_excl_tax: float
    tax_amount: float
    total_amount: float
    remark: str = ""


def resolve_cjk_font_file() -> str | None:
    """Return the first available CJK font file across Linux Docker and Windows hosts."""
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def _draw_text(
    page: pymupdf.Page,
    x: float,
    y: float,
    text: str,
    fontname: str,
    size: float = 9.5,
    color: tuple[float, float, float] = (0.08, 0.1, 0.13),
) -> None:
    page.insert_text((x, y), text, fontname=fontname, fontsize=size, color=color)


def render_invoice_pdf_bytes(spec: SampleInvoiceSpec) -> bytes:
    """Render a styled Chinese VAT electronic invoice PDF with clean extractable text."""
    doc = pymupdf.open()
    page = doc.new_page(width=620, height=400)
    font_file = resolve_cjk_font_file()
    fontname = "cjk" if font_file else "china-s"
    if font_file:
        page.insert_font(fontname=fontname, fontfile=font_file)

    red = (0.68, 0.15, 0.15)
    navy = (0.06, 0.25, 0.52)
    ink = (0.08, 0.1, 0.14)
    muted = (0.35, 0.39, 0.45)

    # Outer paper & header title
    page.draw_rect(pymupdf.Rect(16, 16, 604, 384), color=red, width=1.1)
    _draw_text(page, 210, 44, "上海增值税电子普通发票", fontname, size=15, color=red)
    page.draw_line((205, 50), (415, 50), color=red, width=0.8)
    page.draw_line((205, 53), (415, 53), color=red, width=0.5)

    # Tax bureau oval stamp mock
    page.draw_circle((310, 42), 20, color=red, width=0.7)

    # Invoice identifiers on top-right
    _draw_text(page, 425, 36, f"发票代码: {spec.invoice_code}", fontname, size=8.8, color=navy)
    _draw_text(page, 425, 51, f"发票号码: {spec.invoice_number}", fontname, size=8.8, color=navy)
    _draw_text(page, 425, 66, f"开票日期: {spec.issue_date}", fontname, size=8.8, color=navy)

    # Buyer block
    y_buyer_top = 80
    page.draw_rect(pymupdf.Rect(24, y_buyer_top, 596, 132), color=red, width=0.7)
    page.draw_line((52, y_buyer_top), (52, 132), color=red, width=0.6)
    _draw_text(page, 31, 102, "购", fontname, size=9, color=red)
    _draw_text(page, 31, 118, "方", fontname, size=9, color=red)

    _draw_text(page, 62, 99, f"购买方名称: {spec.buyer_name}", fontname, size=9.5, color=ink)
    _draw_text(
        page,
        62,
        118,
        f"纳税人识别号: {spec.buyer_tax_id or '(无)'}",
        fontname,
        size=9.5,
        color=ink,
    )
    _draw_text(page, 430, 99, f"费用类别: {spec.category}", fontname, size=9.2, color=navy)

    # Line items table
    y_tbl_top = 138
    y_tbl_bot = 258
    page.draw_rect(pymupdf.Rect(24, y_tbl_top, 596, y_tbl_bot), color=red, width=0.7)
    page.draw_line((24, 162), (596, 162), color=red, width=0.6)
    for col_x in (260, 340, 420, 505):
        page.draw_line((col_x, y_tbl_top), (col_x, y_tbl_bot), color=red, width=0.5)

    headers = [
        (34, "货物或应税劳务、服务名称"),
        (272, "规格/类型"),
        (355, "不含税金额"),
        (445, "税额"),
        (522, "含税合计"),
    ]
    for hx, htext in headers:
        _draw_text(page, hx, 154, htext, fontname, size=8.8, color=red)

    _draw_text(page, 34, 186, spec.item_name, fontname, size=9.8, color=ink)
    _draw_text(page, 272, 186, spec.category, fontname, size=9.5, color=ink)
    _draw_text(page, 355, 186, f"{spec.amount_excl_tax:.2f}", fontname, size=9.5, color=ink)
    _draw_text(page, 445, 186, f"{spec.tax_amount:.2f}", fontname, size=9.5, color=ink)
    _draw_text(page, 522, 186, f"{spec.total_amount:.2f}", fontname, size=9.5, color=ink)

    _draw_text(
        page,
        34,
        242,
        f"合 计: 金额 ¥{spec.amount_excl_tax:.2f}   税额 ¥{spec.tax_amount:.2f}",
        fontname,
        size=9.2,
        color=muted,
    )

    # Total bar
    page.draw_rect(pymupdf.Rect(24, 264, 596, 294), color=red, width=0.7)
    _draw_text(page, 34, 283, "价税合计（大写/小写）:", fontname, size=9.5, color=red)
    _draw_text(
        page,
        210,
        283,
        f"价税合计(小写)：¥{spec.total_amount:.2f}",
        fontname,
        size=10.8,
        color=navy,
    )

    # Seller block + remark
    page.draw_rect(pymupdf.Rect(24, 300, 596, 362), color=red, width=0.7)
    page.draw_line((52, 300), (52, 362), color=red, width=0.6)
    _draw_text(page, 31, 326, "销", fontname, size=9, color=red)
    _draw_text(page, 31, 344, "方", fontname, size=9, color=red)

    _draw_text(page, 62, 324, f"销售方名称: {spec.seller_name}", fontname, size=9.5, color=ink)
    _draw_text(
        page,
        62,
        346,
        f"备注: {spec.remark or '电子普票演示样本'}",
        fontname,
        size=8.8,
        color=muted,
    )

    return doc.tobytes()


def render_invoice_thumbnail(pdf_bytes: bytes, dpi: int = 135) -> bytes:
    """Render first page of an invoice PDF into a PNG thumbnail."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def generate_sample_invoice_specs(
    biz_date: str,
    company_name: str,
    company_tax_id: str,
) -> list[SampleInvoiceSpec]:
    """Build 6 deterministic demo invoices mixing compliant receipts and common audit risks."""
    try:
        ref_day = datetime.strptime(biz_date, "%Y-%m-%d").date()
    except ValueError:
        ref_day = date.today()

    seed = int(hashlib.md5(biz_date.encode("utf-8")).hexdigest()[:6], 16)
    base_no = 88200000 + (seed % 90000)

    d_recent_1 = ref_day.isoformat()
    d_recent_2 = (ref_day - timedelta(days=1)).isoformat()
    d_recent_3 = (ref_day - timedelta(days=2)).isoformat()
    d_expired = (ref_day - timedelta(days=48)).isoformat()

    return [
        SampleInvoiceSpec(
            filename="inv_01_didi_transport.pdf",
            invoice_code="031002500111",
            invoice_number=f"{base_no + 1:08d}",
            issue_date=d_recent_1,
            buyer_name=company_name,
            buyer_tax_id=company_tax_id,
            seller_name="滴滴出行科技有限公司",
            item_name="*运输服务*客运服务费",
            category="交通",
            amount_excl_tax=148.00,
            tax_amount=4.44,
            total_amount=152.44,
            remark="客户拜访市区专车交通费",
        ),
        SampleInvoiceSpec(
            filename="inv_02_quanji_hotel.pdf",
            invoice_code="031002500111",
            invoice_number=f"{base_no + 2:08d}",
            issue_date=d_recent_2,
            buyer_name=company_name,
            buyer_tax_id=company_tax_id,
            seller_name="全季酒店管理有限公司陆家嘴店",
            item_name="*住宿服务*商务标准间住宿费",
            category="住宿",
            amount_excl_tax=433.96,
            tax_amount=26.04,
            total_amount=460.00,
            remark="出差住宿 1 晚（标准上限 500 元）",
        ),
        SampleInvoiceSpec(
            filename="inv_03_jd_office.pdf",
            invoice_code="011002500211",
            invoice_number=f"{base_no + 3:08d}",
            issue_date=d_recent_3,
            buyer_name=company_name,
            buyer_tax_id=company_tax_id,
            seller_name="北京京东世纪信息技术有限公司",
            item_name="*办公用品*打印纸与无线键鼠套装",
            category="办公",
            amount_excl_tax=291.15,
            tax_amount=37.85,
            total_amount=329.00,
            remark="项目组日常办公耗材采购",
        ),
        SampleInvoiceSpec(
            filename="inv_04_haidilao_overlimit.pdf",
            invoice_code="031002500111",
            invoice_number=f"{base_no + 4:08d}",
            issue_date=d_recent_1,
            buyer_name=company_name,
            buyer_tax_id=company_tax_id,
            seller_name="海底捞餐饮股份有限公司第一分公司",
            item_name="*餐饮服务*聚餐餐费",
            category="餐饮",
            amount_excl_tax=641.51,
            tax_amount=38.49,
            total_amount=680.00,
            remark="超出单笔餐饮报销限额（>200元）",
        ),
        SampleInvoiceSpec(
            filename="inv_05_starbucks_wrong_title.pdf",
            invoice_code="031002500111",
            invoice_number=f"{base_no + 5:08d}",
            issue_date=d_recent_2,
            buyer_name="个人",
            buyer_tax_id="",
            seller_name="上海星巴克咖啡经营有限公司",
            item_name="*餐饮服务*饮品与简餐",
            category="餐饮",
            amount_excl_tax=90.57,
            tax_amount=5.43,
            total_amount=96.00,
            remark="抬头为个人且缺少公司税号",
        ),
        SampleInvoiceSpec(
            filename="inv_06_duplicate_expired.pdf",
            invoice_code="031002500111",
            invoice_number=f"{base_no + 1:08d}",  # 刻意与第 1 张重复
            issue_date=d_expired,
            buyer_name=company_name,
            buyer_tax_id=company_tax_id,
            seller_name="滴滴出行科技有限公司",
            item_name="*运输服务*客运服务费",
            category="交通",
            amount_excl_tax=148.00,
            tax_amount=4.44,
            total_amount=152.44,
            remark="疑似重复报销且开票日期超过30天",
        ),
    ]


def populate_sample_invoices(
    target_dir: Path,
    biz_date: str,
    company_name: str,
    company_tax_id: str,
    overwrite: bool = False,
) -> list[Path]:
    """Generate sample invoice PDFs into target_dir (replacing old inv_0*.pdf if overwrite)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for old in target_dir.glob("inv_0*.pdf"):
            old.unlink(missing_ok=True)

    specs = generate_sample_invoice_specs(biz_date, company_name, company_tax_id)
    created: list[Path] = []
    for spec in specs:
        dest = target_dir / spec.filename
        if overwrite or not dest.exists():
            dest.write_bytes(render_invoice_pdf_bytes(spec))
        created.append(dest)
    return created


def remove_matching_invoices(paths: Iterable[Path]) -> None:
    for p in paths:
        p.unlink(missing_ok=True)
