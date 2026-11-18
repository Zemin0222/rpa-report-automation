from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import openpyxl
import uvicorn
from fastapi.testclient import TestClient

import app.web_console as wc
from core.config_loader import ExpenseConfig, StoragePaths, load_config
from core.pipeline import run_daily_pipeline
from mock_systems.invoice_factory import populate_sample_invoices
from mock_systems.server import app as mock_app
from processors.expense_rules import audit_invoices
from processors.invoice_extractor import extract_invoice_from_file, parse_invoice_text


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
        invoices=tmp_path / "invoices",
    )
    cfg.storage.ensure_all()
    cfg.notifier.dry_run = True
    cfg.browser.headless = True
    return cfg


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_invoice_factory_and_pdf_extraction(tmp_path: Path) -> None:
    exp_cfg = ExpenseConfig()
    thumb_dir = tmp_path / ".thumbs"
    files = populate_sample_invoices(
        tmp_path,
        "2026-11-18",
        exp_cfg.company_name,
        exp_cfg.company_tax_id,
    )
    assert len(files) == 6

    records = [extract_invoice_from_file(p, thumb_dir=thumb_dir) for p in files]
    assert len(records) == 6
    assert records[0].seller_name.startswith("滴滴出行")
    assert records[0].category == "交通"
    assert abs(records[0].total_amount - 152.44) < 0.01
    assert records[0].buyer_name == exp_cfg.company_name
    assert records[0].buyer_tax_id == exp_cfg.company_tax_id
    assert records[0].confidence >= 0.9
    assert (thumb_dir / "inv_01_didi_transport.png").exists()


def test_expense_audit_rules_detects_all_risk_types(tmp_path: Path) -> None:
    exp_cfg = ExpenseConfig()
    files = populate_sample_invoices(
        tmp_path,
        "2026-11-18",
        exp_cfg.company_name,
        exp_cfg.company_tax_id,
    )
    records = [extract_invoice_from_file(p) for p in files]
    audit = audit_invoices(records, "2026-11-18", exp_cfg)

    assert audit.total_count == 6
    assert audit.approved_count == 3
    assert audit.flagged_count == 3
    assert abs(audit.approved_amount - (152.44 + 460.0 + 329.0)) < 0.01

    risk_map = {item.record.file_name: "；".join(item.risk_reasons) for item in audit.flagged_items}
    assert "超标" in risk_map["inv_04_haidilao_overlimit.pdf"]
    assert "抬头" in risk_map["inv_05_starbucks_wrong_title.pdf"]
    assert "重复" in risk_map["inv_06_duplicate_expired.pdf"]
    assert "超期" in risk_map["inv_06_duplicate_expired.pdf"]


def test_parse_invoice_normalizes_cjk_compatibility_glyphs() -> None:
    """Ensure Noto Sans CJK compatibility codepoints (U+F971/U+F90A/U+2011) normalize cleanly."""
    noto_text = (
        "发票代码: 031002500111\n"
        "发票号码: 88270109\n"
        "开票日期: 2026\u201111\u201118\n"
        "购买方名称: 星\uf971智联科技有限公司 费用类别: 交通\n"
        "纳税人识别号: 91310115MA1K88990X\n"
        "合 计: \uf90a额 ¥148.00 税额 ¥4.44\n"
        "价税合计(小写)：¥152.44\n"
        "销售方名称: 滴滴出\ufa08科技有限公司\n"
    )
    rec = parse_invoice_text(noto_text)
    assert rec.issue_date == "2026-11-18"
    assert rec.buyer_name == "星辰智联科技有限公司"
    assert rec.seller_name == "滴滴出行科技有限公司"
    assert abs(rec.amount_excl_tax - 148.00) < 0.01
    assert abs(rec.total_amount - 152.44) < 0.01


def test_expense_workflow_e2e_playwright_and_console_api(tmp_path: Path, monkeypatch) -> None:
    port = _find_free_port()
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)

    try:
        cfg = _isolated_cfg(tmp_path)
        cfg.connectors.mock_expense.base_url = f"http://127.0.0.1:{port}"

        outcome = run_daily_pipeline(
            biz_date="2026-11-18",
            force=True,
            dry_run=True,
            cfg=cfg,
            job_name="expense_reimburse",
        )
        assert outcome.status == "SUCCESS"
        assert outcome.row_count == 6
        assert outcome.anomaly_count == 3
        assert outcome.oa_ticket_id is not None and outcome.oa_ticket_id.startswith("EXP-20261118-")
        assert outcome.excel_path is not None and outcome.excel_path.exists()
        assert outcome.notify_preview_path is not None and outcome.notify_preview_path.exists()

        wb = openpyxl.load_workbook(outcome.excel_path)
        assert set(wb.sheetnames) == {"报销汇总总览", "合规自动填报明细", "风控拦截清单"}

        # Verify Web Console API exposes expense workflow state, thumbnail, and Excel download
        monkeypatch.setattr(wc, "load_config", lambda: cfg)
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        client = TestClient(wc.app)

        st = client.get("/api/state?job_name=expense_reimburse")
        assert st.status_code == 200
        payload = st.json()
        assert payload["selected_job"] == "expense_reimburse"
        assert payload["active_workflow"]["view_type"] == "expense"
        assert len(payload["active_run"]["kpi"]["invoices"]) == 6

        dl = client.get("/api/download/expense_excel/2026-11-18")
        assert dl.status_code == 200

        thumb = client.get("/api/expense/thumb/inv_01_didi_transport.png")
        assert thumb.status_code == 200 and thumb.content.startswith(b"\x89PNG")

        gen = client.post("/api/expense/samples?biz_date=2026-11-18")
        assert gen.status_code == 200 and gen.json()["generated"] == 6
    finally:
        server.should_exit = True
        thread.join(timeout=3)
