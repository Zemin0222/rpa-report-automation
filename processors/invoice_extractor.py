from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal

import pdfplumber
from pydantic import BaseModel

from core.config_loader import LlmConfig
from core.logger import get_logger
from mock_systems.invoice_factory import render_invoice_thumbnail

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
    extract_source: Literal["ai", "rule"] = "rule"
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
) -> InvoiceRecord:
    """Optionally enhance/extract invoice JSON using OpenAI-compatible Vision/Text API."""
    if llm_cfg is None or not llm_cfg.enabled:
        return base_record
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return base_record

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.getenv("OPENAI_MODEL", "").strip() or llm_cfg.model
    prompt = (
        "你是企业财务发票审核助手，请从下面发票文本或图片中抽取以下 JSON 字段："
        "invoice_code, invoice_number, issue_date(YYYY-MM-DD), buyer_name, buyer_tax_id, "
        "seller_name, item_name, category(交通/住宿/餐饮/办公/通信), "
        "amount_excl_tax(float), tax_amount(float), total_amount(float)。仅返回合法 JSON。"
    )
    content: list[dict[str, object]] = [{"type": "text", "text": f"{prompt}\n\n文本内容:\n{raw_text[:2500]}"}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)
        resp = client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )
        raw_out = (resp.choices[0].message.content or "").strip()
        json_match = re.search(r"\{.*\}", raw_out, re.DOTALL)
        if not json_match:
            return base_record
        parsed = json.loads(json_match.group(0))
        return base_record.model_copy(
            update={
                "invoice_code": str(parsed.get("invoice_code") or base_record.invoice_code),
                "invoice_number": str(parsed.get("invoice_number") or base_record.invoice_number),
                "issue_date": _normalize_date_str(str(parsed.get("issue_date") or "")) or base_record.issue_date,
                "buyer_name": str(parsed.get("buyer_name") or base_record.buyer_name),
                "buyer_tax_id": str(parsed.get("buyer_tax_id") or base_record.buyer_tax_id),
                "seller_name": str(parsed.get("seller_name") or base_record.seller_name),
                "item_name": str(parsed.get("item_name") or base_record.item_name),
                "category": infer_category(
                    str(parsed.get("seller_name") or ""),
                    explicit=str(parsed.get("category") or base_record.category),
                ),
                "amount_excl_tax": float(parsed.get("amount_excl_tax") or base_record.amount_excl_tax),
                "tax_amount": float(parsed.get("tax_amount") or base_record.tax_amount),
                "total_amount": float(parsed.get("total_amount") or base_record.total_amount),
                "extract_source": "ai",
                "confidence": 0.96,
            }
        )
    except Exception as exc:
        logger.warning("AI invoice extraction failed (%s); falling back to rule parser.", exc)
        return base_record


def extract_invoice_from_file(
    path: Path,
    llm_cfg: LlmConfig | None = None,
    thumb_dir: Path | None = None,
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
    return _try_llm_extract_invoice(extracted_text, thumb_bytes, llm_cfg, base_rec)
