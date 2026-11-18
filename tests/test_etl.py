from __future__ import annotations

import pandas as pd
from core.config_loader import ThresholdConfig
from processors.etl import normalize_channel, run_etl


def test_normalize_channel_aliases() -> None:
    assert normalize_channel("douyin") == "抖音信息流"
    assert normalize_channel("XHS") == "小红书种草"
    assert normalize_channel("  百度 ") == "百度搜索"


def test_etl_merges_and_flags_anomalies() -> None:
    orders = pd.DataFrame(
        [
            {
                "order_id": "O-1",
                "biz_date": "2026-11-17",
                "channel": "抖音",
                "customer_tier": "新客",
                "order_amount": 500.0,
                "refund_amount": 0.0,
                "order_status": "已成交",
            },
            {
                "order_id": "O-1",  # duplicate order_id should be deduplicated
                "biz_date": "2026-11-17",
                "channel": "抖音",
                "customer_tier": "新客",
                "order_amount": 500.0,
                "refund_amount": 0.0,
                "order_status": "已成交",
            },
            {
                "order_id": "O-2",
                "biz_date": "2026-11-17",
                "channel": "小红书",
                "customer_tier": "新客",
                "order_amount": 200.0,
                "refund_amount": 100.0,
                "order_status": "已退款",
            },
            {
                "order_id": "O-3",
                "biz_date": "2026-11-17",
                "channel": "自然流量",
                "customer_tier": "VIP",
                "order_amount": 300.0,
                "refund_amount": 0.0,
                "order_status": "已成交",
            },
        ]
    )
    ads = pd.DataFrame(
        [
            {"channel": "抖音信息流", "impressions": 10000, "clicks": 500, "ad_spend": 100.0},
            {"channel": "小红书种草", "impressions": 10000, "clicks": 100, "ad_spend": 400.0},
        ]
    )

    result = run_etl(
        biz_date="2026-11-17",
        orders_df=orders,
        ads_df=ads,
        drop_df=None,
        thresholds=ThresholdConfig(min_roi=2.0, max_sales_drop_pct=20.0, max_refund_rate_pct=15.0),
        prev_channel_sales={"抖音信息流": 1000.0},
    )

    assert result.kpis["total_orders"] == 3
    assert result.kpis["total_net_sales"] == 900.0
    assert result.kpis["total_ad_spend"] == 500.0
    assert result.kpis["overall_roi"] == 1.8

    # 小红书种草: ROI = 100 / 400 = 0.25 (<2.0) and refund_rate = 50% (>15%)
    # 抖音信息流: net_sales 500 vs prev 1000 -> -50% (<-20%)
    # 自然流量: ad_spend == 0 -> should not trigger ROI < 2.0 alarm
    anomaly_channels = {a["channel"] for a in result.anomalies}
    assert "小红书种草" in anomaly_channels
    assert "抖音信息流" in anomaly_channels
    assert "自然流量" not in anomaly_channels


def test_etl_three_way_reconciliation() -> None:
    orders = pd.DataFrame(
        [
            {"order_id": "O-10", "biz_date": "2026-11-17", "channel": "抖音", "order_amount": 300.0, "refund_amount": 0.0},
            {"order_id": "O-11", "biz_date": "2026-11-17", "channel": "百度", "order_amount": 200.0, "refund_amount": 0.0},
            {"order_id": "O-12", "biz_date": "2026-11-17", "channel": "微信", "order_amount": 150.0, "refund_amount": 0.0},
        ]
    )
    finance = pd.DataFrame(
        [
            {"order_id": "O-10", "settled_amount": 300.0, "status": "SETTLED"},
            {"order_id": "O-11", "settled_amount": 182.0, "status": "DISPUTED"},
            {"order_id": "O-99", "settled_amount": 99.0, "status": "UNMATCHED"},
        ]
    )
    res = run_etl(
        biz_date="2026-11-17",
        orders_df=orders,
        ads_df=pd.DataFrame(),
        drop_df=None,
        thresholds=ThresholdConfig(),
        finance_df=finance,
    )
    assert res.reconcile_summary["enabled"] is True
    assert res.reconcile_summary["matched_count"] == 1
    assert res.reconcile_summary["discrepancy_count"] == 3
    diff_types = set(res.reconcile_df["diff_type"].tolist())
    assert "CRM已成交未入账(漏单)" in diff_types
    assert "财务多出结算(长款)" in diff_types
    assert "结算金额不符(短款/扣费差异)" in diff_types
