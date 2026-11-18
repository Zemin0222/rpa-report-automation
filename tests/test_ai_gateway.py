from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from ai.gateway import AiGateway
from core.config_loader import LlmConfig, ThresholdConfig
from mock_systems.invoice_factory import populate_sample_invoices
from processors.etl import run_etl
from processors.invoice_extractor import extract_invoice_from_file
from processors.summary_builder import build_summary
from storage.state_db import StateDB


class _FakeCompletions:
    def __init__(self, outputs: list[str], tokens_per_call: int = 120) -> None:
        self._outputs = list(outputs)
        self._tokens = tokens_per_call
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._outputs) - 1)
        content = self._outputs[idx]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=self._tokens // 2,
                completion_tokens=self._tokens - self._tokens // 2,
                total_tokens=self._tokens,
            ),
        )


def _fake_client(outputs: list[str], tokens_per_call: int = 120):
    completions = _FakeCompletions(outputs, tokens_per_call=tokens_per_call)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_ai_gateway_explain_and_sqlite_cache_hit(tmp_path) -> None:
    db = StateDB(tmp_path / "state.db")
    cfg = LlmConfig(enabled=True, model="gpt-4o-mini", daily_call_budget=10, daily_token_budget=5000)
    payload = json.dumps(
        {
            "channel": "抖音直播",
            "root_cause": "短视频素材CTR衰減导致获客成本上升",
            "impact": "净成交转化低于盈亏平衡线",
            "recommended_actions": ["暂停低效广告组", "上新3条口播对比创意"],
            "urgency": "高",
        },
        ensure_ascii=False,
    )
    client, calls = _fake_client([payload], tokens_per_call=180)
    gw = AiGateway(cfg, db=db, client=client)

    row = {
        "channel": "抖音直播",
        "net_sales": 12000.0,
        "ad_spend": 8500.0,
        "roi": 1.41,
        "refund_rate_pct": 18.2,
        "sales_dod_pct": -24.0,
        "anomaly_reason": "ROI低于阈值",
    }

    r1 = gw.explain_anomaly(row, trend_context=[{"biz_date": "2026-11-17", "total_net_sales": 15800}])
    assert r1.source == "ai"
    assert r1.tokens == 180
    assert r1.data.urgency == "高"
    assert len(calls.calls) == 1

    # Identical call must hit SQLite semantic cache with 0 extra LLM requests
    r2 = gw.explain_anomaly(row, trend_context=[{"biz_date": "2026-11-17", "total_net_sales": 15800}])
    assert r2.source == "cache"
    assert r2.data.root_cause == r1.data.root_cause
    assert len(calls.calls) == 1

    stats = db.get_ai_daily_stats()
    assert stats["calls_today"] == 1
    assert stats["tokens_today"] == 180
    assert stats["cache_hits_today"] == 1
    assert stats["cache_hit_rate_pct"] == 50.0


def test_ai_gateway_schema_repair_retry_then_fallback(tmp_path) -> None:
    db = StateDB(tmp_path / "state.db")
    cfg = LlmConfig(enabled=True, model="gpt-4o-mini", max_retries=1)

    # First attempt invalid (missing required fields), repair attempt succeeds
    repaired_json = json.dumps(
        {
            "order_id": "ORD-9001",
            "likely_causes": ["支付回调跨日延迟"],
            "check_steps": ["导出微信商户号 T+1 对账单核销"],
            "priority": "P1",
        },
        ensure_ascii=False,
    )
    client_ok, calls_ok = _fake_client(["not valid json at all", f"```json\n{repaired_json}\n```"])
    gw_ok = AiGateway(cfg, db=db, client=client_ok)
    item = {
        "order_id": "ORD-9001",
        "diff_type": "CRM已入账-财务漏单",
        "finance_status": "UNSETTLED",
        "diff_amount": 320.0,
        "action_advice": "排查未结算订单",
    }
    res_repaired = gw_ok.attribute_reconcile_diff(item)
    assert res_repaired.source == "ai"
    assert len(calls_ok.calls) == 2
    assert res_repaired.data.priority == "P1"

    # Exhaust retries -> seamless deterministic rule fallback
    client_bad, calls_bad = _fake_client(["broken", "{still_broken"])
    gw_bad = AiGateway(LlmConfig(enabled=True, cache_enabled=False, max_retries=1), db=db, client=client_bad)
    res_fb = gw_bad.attribute_reconcile_diff({**item, "order_id": "ORD-9002"})
    assert res_fb.source == "rule"
    assert len(calls_bad.calls) == 2
    assert res_fb.data.order_id == "ORD-9002"
    assert len(res_fb.data.likely_causes) >= 1
    assert len(res_fb.data.check_steps) >= 1


def test_ai_gateway_budget_tripwires_enforce_rule_fallback(tmp_path) -> None:
    db = StateDB(tmp_path / "state.db")
    reconcile_ok = json.dumps(
        {
            "order_id": "ORD-1",
            "likely_causes": ["通道手续费差额"],
            "check_steps": ["核对分账费率"],
            "priority": "P2",
        },
        ensure_ascii=False,
    )
    # 1) Call budget cap = 1
    cfg = LlmConfig(enabled=True, cache_enabled=False, daily_call_budget=1, daily_token_budget=50000)
    client, calls = _fake_client([reconcile_ok], tokens_per_call=150)
    gw = AiGateway(cfg, db=db, client=client)

    first = gw.attribute_reconcile_diff({"order_id": "ORD-1", "diff_type": "金额不符", "diff_amount": 5.0})
    second = gw.attribute_reconcile_diff({"order_id": "ORD-2", "diff_type": "长款", "diff_amount": -50.0})
    assert first.source == "ai"
    assert second.source == "rule"
    assert second.fallback_reason == "daily_call_budget_exceeded"
    assert len(calls.calls) == 1

    # 2) Token budget cap reached even with higher call budget
    cfg_tok = LlmConfig(enabled=True, cache_enabled=False, daily_call_budget=10, daily_token_budget=100)
    gw_tok = AiGateway(cfg_tok, db=db, client=client)
    third = gw_tok.attribute_reconcile_diff({"order_id": "ORD-3", "diff_type": "漏单", "diff_amount": 99.0})
    assert third.source == "rule"
    assert third.fallback_reason == "daily_token_budget_exceeded"
    assert len(calls.calls) == 1


def test_domain_integrations_summary_and_invoice_cache(tmp_path) -> None:
    db = StateDB(tmp_path / "state.db")

    # Summary enrichment via AiGateway
    orders_df = pd.DataFrame(
        [
            {
                "order_id": "ORD-1",
                "channel": "天猫",
                "order_amount": 1000.0,
                "refund_amount": 50.0,
                "status": "PAID",
            }
        ]
    )
    ads_df = pd.DataFrame([{"channel": "天猫", "ad_spend": 600.0, "clicks": 300, "impressions": 5000}])
    etl = run_etl("2026-11-18", orders_df, ads_df, pd.DataFrame(), ThresholdConfig(), prev_channel_sales={})

    advice_json = json.dumps(
        {
            "highlights": ["天猫通道整体转化稳定"],
            "suggestions": ["重点复盘 ROI 低于 2.0 的推广词包", "核查退款原因标签"],
        },
        ensure_ascii=False,
    )
    gw_sum = AiGateway(LlmConfig(enabled=True), db=db, client=_fake_client([advice_json])[0])
    enriched = build_summary(etl, LlmConfig(enabled=True), gateway=gw_sum)
    assert "AI 运营建议" in enriched
    assert "重点复盘 ROI" in enriched

    # Disabled LLM uses deterministic rule summary cleanly
    plain = build_summary(etl, LlmConfig(enabled=False))
    assert "AI 运营建议" not in plain

    # Invoice structured extraction hits AI first, then cache
    inv_dir = tmp_path / "invoices"
    files = populate_sample_invoices(inv_dir, "2026-11-18", "星辰智算科技有限公司", "91310115MA1K8X290P", overwrite=True)
    inv_json = json.dumps(
        {
            "invoice_code": "031002600111",
            "invoice_number": "88991100",
            "issue_date": "2026-11-18",
            "buyer_name": "星辰智算科技有限公司",
            "buyer_tax_id": "91310115MA1K8X290P",
            "seller_name": "上海携程国际旅行社有限公司",
            "item_name": "*客运服务*高铁客票",
            "category": "交通",
            "amount_excl_tax": 500.0,
            "tax_amount": 45.0,
            "total_amount": 545.0,
        },
        ensure_ascii=False,
    )
    inv_client, inv_calls = _fake_client([inv_json], tokens_per_call=210)
    gw_inv = AiGateway(LlmConfig(enabled=True), db=db, client=inv_client)

    rec1 = extract_invoice_from_file(files[0], llm_cfg=LlmConfig(enabled=True), gateway=gw_inv)
    assert rec1.extract_source == "ai"
    assert rec1.confidence == 0.96
    assert len(inv_calls.calls) == 1

    rec2 = extract_invoice_from_file(files[0], llm_cfg=LlmConfig(enabled=True), gateway=gw_inv)
    assert rec2.extract_source == "cache"
    assert len(inv_calls.calls) == 1
