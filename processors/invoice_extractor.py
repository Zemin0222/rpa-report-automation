from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pdfplumber
from pydantic import BaseModel

from ai.schemas import InvoiceExtractionSchema
from core.config_loader import LlmConfig
from core.logger import get_logger
from mock_systems.invoice_factory import render_invoice_thumbnail

if TYPE_CHECKING:
    from ai.gateway import AiGateway

logger = get_logger("rpa.invoice_extractor")


class InvoiceRecord(BaseModel):
    """Structured invoice entity extracted from PDF or image attachments."""

    file_name: str
    file_path: str
    file_hash: str
    invoice_code: str = ""
    invoice_number: str = ""
    issue_date: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    seller_name: str = ""
    item_name: str = ""
    category: str = "办公"
    amount_excl_tax: float = 0.0
    tax_amount: float = 0.0
    total_amount: float = 0.0
    extract_source: Literal["ai", "cache", "rule"] = "rule"
    confidence: float = 0.0
    thumbnail_url: str = ""


_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("交通", ("交通", "运输服务", "客运", "滴滴", "出租", "打车", "高铁", "火车", "机票", "航旅", "出行")),
    ("住宿", ("住宿", "酒店", "宾馆", "全季", "如家", "汉庭", "亚朵", "民宿")),
    ("餐饮", ("餐饮", "餐费", "聚餐", "海底捞", "星巴克", "咖啡", "食品", "美食", "奶茶")),
    ("办公", ("办公用品", "京东", "打印", "耗材", "文具", "键鼠", "办公")),
    ("通信", ("通信", "电信", "移动", "联通", "话费", "宽带")),
]


def infer_category(text: str, explicit: str = "") -> str:
    combined = f"{explicit} {text}"
    if explicit.strip() in {"交通", "住宿", "餐饮", "办公", "通信"}:
        return explicit.strip()
    for category, keywords in _CATEGORY_RULES:
        if any(kw in combined for kw in keywords):
            return category
    return "办公"


def _normalize_date_str(raw: str) -> str:
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", raw)
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_invoice_text(
    text: str,
    file_name: str = "invoice.pdf",
    file_path: str = "",
    file_hash: str = "",
) -> InvoiceRecord:
    """Parse Chinese VAT electronic invoice fields deterministically from extracted PDF text."""
    # NFKC unifies CJK Compatibility Ideographs (e.g. Noto Sans CJK U+F971 -> 辰, U+F90A -> 金)
    normalized = unicodedata.normalize("NFKC", text or "")
    clean = re.sub(r"[ \t]+", " ", normalized)

    code_m = re.search(r"发票代码[:：\s]*([0-9]{10,12})", clean)
    num_m = re.search(r"发票号码[:：\s]*([0-9]{8,20})", clean)
    date_m = re.search(r"开票日期[:：\s]*([0-9]{4}\D[0-9]{1,2}\D[0-9]{1,2})", clean)
    buyer_m = re.search(r"购买方名称[:：\s]*([^\n]+)", clean)
    tax_id_m = re.search(r"纳税人识别号[:：\s]*([A-Za-z0-9()无-]+)", clean)
    seller_m = re.search(r"销售方名称[:：\s]*([^\n]+)", clean)
    cat_m = re.search(r"费用类别[:：\s]*([^\n\s]+)", clean)
    total_m = re.search(r"价税合计\s*[（(]小写[)）][:：\s]*[¥￥]?\s*([0-9]+(?:\.[0-9]{1,2})?)", clean)
    excl_m = re.search(r"金额\s*[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)", clean)
    tax_amt_m = re.search(r"税额\s*[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)", clean)
    item_m = re.search(r"(\*[^*\n]+\*[^\s\n]+)", clean)

    buyer_name = buyer_m.group(1).split("费用类别")[0].strip() if buyer_m else ""
    tax_raw = tax_id_m.group(1).strip() if tax_id_m else ""
    buyer_tax_id = "" if ("无" in tax_raw or not tax_raw) else tax_raw
    seller_name = seller_m.group(1).strip() if seller_m else ""
    explicit_cat = cat_m.group(1).strip() if cat_m else ""
    item_name = item_m.group(1).strip() if item_m else ""

    total_val = float(total_m.group(1)) if total_m else 0.0
    excl_val = float(excl_m.group(1)) if excl_m else round(total_val / 1.06, 2)
    tax_val = float(tax_amt_m.group(1)) if tax_amt_m else round(max(0.0, total_val - excl_val), 2)

    category = infer_category(f"{seller_name} {item_name} {clean}", explicit=explicit_cat)

    populated = sum(
        bool(v)
        for v in (
            code_m,
            num_m,
            date_m,
            buyer_name,
            seller_name,
            total_val > 0,
        )
    )
    confidence = round(0.42 + 0.095 * populated, 2) if populated > 0 else 0.25

    return InvoiceRecord(
        file_name=file_name,
        file_path=file_path,
        file_hash=file_hash,
        invoice_code=code_m.group(1) if code_m else "",
        invoice_number=num_m.group(1) if num_m else "",
        issue_date=_normalize_date_str(date_m.group(1)) if date_m else "",
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        seller_name=seller_name,
        item_name=item_name or f"*{category}费用*",
        category=category,
        amount_excl_tax=excl_val,
        tax_amount=tax_val,
        total_amount=total_val,
        extract_source="rule",
        confidence=min(0.99, confidence),
    )


def _try_llm_extract_invoice(
    raw_text: str,
    image_bytes: bytes | None,
    llm_cfg: LlmConfig | None,
    base_record: InvoiceRecord,
    gateway: AiGateway | None = None,
) -> InvoiceRecord:
    """Optionally enhance invoice extraction via AiGateway (cache + budget guard + rule fallback)."""
    from ai.gateway import AiGateway

    effective_cfg = llm_cfg or LlmConfig(enabled=False)
    gw = gateway or AiGateway(effective_cfg)
    if not effective_cfg.enabled and gw.injected_client is None:
        return base_record

    fallback_cat: Literal["交通", "住宿", "餐饮", "办公", "通信"] = (
        base_record.category
        if base_record.category in {"交通", "住宿", "餐饮", "办公", "通信"}
        else "办公"
    )  # type: ignore[assignment]
    fallback_schema = InvoiceExtractionSchema(
        invoice_code=base_record.invoice_code,
        invoice_number=base_record.invoice_number,
        issue_date=base_record.issue_date,
        buyer_name=base_record.buyer_name,
        buyer_tax_id=base_record.buyer_tax_id,
        seller_name=base_record.seller_name,
        item_name=base_record.item_name,
        category=fallback_cat,
        amount_excl_tax=base_record.amount_excl_tax,
        tax_amount=base_record.tax_amount,
        total_amount=base_record.total_amount,
    )

    res = gw.extract_invoice_structured(
        invoice_text=raw_text,
        fallback_schema=fallback_schema,
        image_bytes=image_bytes,
    )
    if res.source not in {"ai", "cache"}:
        return base_record

    parsed = res.data
    return base_record.model_copy(
        update={
            "invoice_code": parsed.invoice_code or base_record.invoice_code,
            "invoice_number": parsed.invoice_number or base_record.invoice_number,
            "issue_date": _normalize_date_str(parsed.issue_date) or base_record.issue_date,
            "buyer_name": parsed.buyer_name or base_record.buyer_name,
            "buyer_tax_id": parsed.buyer_tax_id or base_record.buyer_tax_id,
            "seller_name": parsed.seller_name or base_record.seller_name,
            "item_name": parsed.item_name or base_record.item_name,
            "category": infer_category(parsed.seller_name, explicit=parsed.category or base_record.category),
            "amount_excl_tax": parsed.amount_excl_tax if parsed.amount_excl_tax > 0 else base_record.amount_excl_tax,
            "tax_amount": parsed.tax_amount if parsed.tax_amount >= 0 else base_record.tax_amount,
            "total_amount": parsed.total_amount if parsed.total_amount > 0 else base_record.total_amount,
            "extract_source": res.source,
            "confidence": 0.96,
        }
    )


def extract_invoice_from_file(
    path: Path,
    llm_cfg: LlmConfig | None = None,
    thumb_dir: Path | None = None,
    gateway: AiGateway | None = None,
) -> InvoiceRecord:
    """Read a PDF or image invoice file, generate PNG preview if needed, and extract structured fields."""
    raw_bytes = path.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    suffix = path.suffix.lower()

    extracted_text = ""
    thumb_bytes: bytes | None = None
    thumb_url = ""

    if suffix == ".pdf":
        with pdfplumber.open(path) as pdf:
            extracted_text = "\n".join((page.extract_text() or "") for page in pdf.pages)
        try:
            thumb_bytes = render_invoice_thumbnail(raw_bytes)
        except Exception as exc:
            logger.warning("Failed rendering invoice thumbnail for %s: %s", path.name, exc)
    elif suffix in {".png", ".jpg", ".jpeg"}:
        thumb_bytes = raw_bytes
        extracted_text = path.stem.replace("_", " ")
    else:
        extracted_text = raw_bytes.decode("utf-8", errors="ignore")

    if thumb_bytes and thumb_dir is not None:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_name = f"{path.stem}.png" if suffix != ".png" else path.name
        (thumb_dir / thumb_name).write_bytes(thumb_bytes)
        thumb_url = f"/api/expense/thumb/{thumb_name}"

    base_rec = parse_invoice_text(
        extracted_text,
        file_name=path.name,
        file_path=str(path),
        file_hash=file_hash,
    )
    base_rec.thumbnail_url = thumb_url
    return _try_llm_extract_invoice(
        extracted_text,
        thumb_bytes,
        llm_cfg,
        base_rec,
        gateway=gateway,
    )
