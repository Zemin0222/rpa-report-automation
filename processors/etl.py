from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import pandas as pd

from core.config_loader import ThresholdConfig

CHANNEL_ALIASES = {
    "douyin": "抖音信息流",
    "抖音": "抖音信息流",
    "baidu": "百度搜索",
    "百度": "百度搜索",
    "xhs": "小红书种草",
    "xiaohongshu": "小红书种草",
    "小红书": "小红书种草",
    "wechat": "微信公众号",
    "微信": "微信公众号",
    "organic": "自然流量",
}


def normalize_channel(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "未分类"
    return CHANNEL_ALIASES.get(raw.lower(), raw)


@dataclass
class EtlResult:
    biz_date: str
    channel_df: pd.DataFrame
    orders_df: pd.DataFrame
    kpis: dict[str, Any]
    anomalies: list[dict[str, Any]]
    reconcile_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    reconcile_summary: dict[str, Any] = field(default_factory=dict)


def _reconcile_orders(
    clean_orders: pd.DataFrame,
    finance_df: pd.DataFrame | None,
    max_diff_yuan: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Compare CRM net order amounts against Finance settlement records by order_id."""
    empty_cols = [
        "order_id",
        "channel",
        "crm_net_amount",
        "settled_amount",
        "diff_amount",
        "diff_type",
        "finance_status",
        "action_advice",
    ]
    if finance_df is None or finance_df.empty or "order_id" not in finance_df.columns:
        return pd.DataFrame(columns=empty_cols), {
            "enabled": False,
            "total_checked": 0,
            "matched_count": 0,
            "discrepancy_count": 0,
            "total_diff_amount": 0.0,
        }

    crm_sub = (
        clean_orders[clean_orders["net_amount"] > 0][["order_id", "channel", "net_amount"]]
        .copy()
        if not clean_orders.empty
        else pd.DataFrame(columns=["order_id", "channel", "net_amount"])
    )
    fin_sub = finance_df.copy()
    fin_sub["order_id"] = fin_sub["order_id"].astype(str).str.strip()
    fin_sub["settled_amount"] = pd.to_numeric(
        fin_sub.get("settled_amount", 0.0), errors="coerce"
    ).fillna(0.0)
    fin_sub = fin_sub.drop_duplicates(subset=["order_id"], keep="last")

    joined = pd.merge(crm_sub, fin_sub, on="order_id", how="outer", indicator=True)
    discrepancy_rows: list[dict[str, Any]] = []
    matched_count = 0

    for _, rec in joined.iterrows():
        merge_flag = rec["_merge"]
        order_id = str(rec["order_id"])
        channel = str(rec["channel"]) if pd.notna(rec.get("channel")) else "未知渠道"
        crm_amt = round(float(rec["net_amount"]), 2) if pd.notna(rec.get("net_amount")) else 0.0
        fin_amt = round(float(rec["settled_amount"]), 2) if pd.notna(rec.get("settled_amount")) else 0.0
        fin_status = str(rec["status"]) if pd.notna(rec.get("status")) else "-"
        diff_amt = round(fin_amt - crm_amt, 2)

        if merge_flag == "left_only":
            discrepancy_rows.append(
                {
                    "order_id": order_id,
                    "channel": channel,
                    "crm_net_amount": crm_amt,
                    "settled_amount": 0.0,
                    "diff_amount": -crm_amt,
                    "diff_type": "CRM已成交未入账(漏单)",
                    "finance_status": "MISSING",
                    "action_advice": "核查支付通道回单是否延迟入账",
                }
            )
        elif merge_flag == "right_only":
            discrepancy_rows.append(
                {
                    "order_id": order_id,
                    "channel": channel,
                    "crm_net_amount": 0.0,
                    "settled_amount": fin_amt,
                    "diff_amount": fin_amt,
                    "diff_type": "财务多出结算(长款)",
                    "finance_status": fin_status,
                    "action_advice": "核查是否历史跨日订单或人工补录单",
                }
            )
        elif abs(diff_amt) > max_diff_yuan:
            discrepancy_rows.append(
                {
                    "order_id": order_id,
                    "channel": channel,
                    "crm_net_amount": crm_amt,
                    "settled_amount": fin_amt,
                    "diff_amount": diff_amt,
                    "diff_type": "结算金额不符(短款/扣费差异)",
                    "finance_status": fin_status,
                    "action_advice": "核对优惠券分摊或通道扣费明细",
                }
            )
        else:
            matched_count += 1

    reconcile_df = pd.DataFrame(discrepancy_rows, columns=empty_cols)
    total_diff = (
        round(float(reconcile_df["diff_amount"].abs().sum()), 2)
        if not reconcile_df.empty
        else 0.0
    )
    summary = {
        "enabled": True,
        "total_checked": len(joined),
        "matched_count": matched_count,
        "discrepancy_count": len(reconcile_df),
        "total_diff_amount": total_diff,
    }
    return reconcile_df, summary


def run_etl(
    biz_date: str,
    orders_df: pd.DataFrame,
    ads_df: pd.DataFrame,
    drop_df: pd.DataFrame | None,
    thresholds: ThresholdConfig,
    prev_channel_sales: dict[str, float] | None = None,
    finance_df: pd.DataFrame | None = None,
) -> EtlResult:
    """Clean, join, aggregate, reconcile, and evaluate anomaly rules across multi-source data."""
    prev_sales = prev_channel_sales or {}

    if orders_df is None or orders_df.empty:
        clean_orders = pd.DataFrame(
            columns=[
                "order_id",
                "biz_date",
                "channel",
                "customer_tier",
                "order_amount",
                "refund_amount",
                "order_status",
                "net_amount",
            ]
        )
    else:
        clean_orders = orders_df.copy()
        clean_orders["order_id"] = clean_orders["order_id"].astype(str).str.strip()
        clean_orders = clean_orders.drop_duplicates(subset=["order_id"], keep="last")
        clean_orders["channel"] = clean_orders["channel"].map(normalize_channel)
        clean_orders["order_amount"] = pd.to_numeric(
            clean_orders["order_amount"], errors="coerce"
        ).fillna(0.0)
        clean_orders["refund_amount"] = pd.to_numeric(
            clean_orders.get("refund_amount", 0.0), errors="coerce"
        ).fillna(0.0)
        clean_orders["net_amount"] = (
            clean_orders["order_amount"] - clean_orders["refund_amount"]
        ).round(2)

    if clean_orders.empty:
        order_agg = pd.DataFrame(
            columns=["channel", "order_count", "gmv", "refund_amount", "net_sales"]
        )
    else:
        order_agg = (
            clean_orders.groupby("channel", as_index=False)
            .agg(
                order_count=("order_id", "count"),
                gmv=("order_amount", "sum"),
                refund_amount=("refund_amount", "sum"),
                net_sales=("net_amount", "sum"),
            )
        )

    if ads_df is None or ads_df.empty:
        ads_agg = pd.DataFrame(
            columns=["channel", "impressions", "clicks", "ad_spend"]
        )
    else:
        clean_ads = ads_df.copy()
        clean_ads["channel"] = clean_ads["channel"].map(normalize_channel)
        for col in ("impressions", "clicks", "ad_spend"):
            clean_ads[col] = pd.to_numeric(clean_ads[col], errors="coerce").fillna(0)
        ads_agg = (
            clean_ads.groupby("channel", as_index=False)
            .agg(
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
                ad_spend=("ad_spend", "sum"),
            )
        )

    merged = pd.merge(order_agg, ads_agg, on="channel", how="outer").fillna(0)

    if drop_df is not None and not drop_df.empty and "channel" in drop_df.columns:
        extra = drop_df.copy()
        if "biz_date" in extra.columns:
            extra = extra[extra["biz_date"].astype(str) == biz_date]
        if not extra.empty and "extra_cost" in extra.columns:
            extra["channel"] = extra["channel"].map(normalize_channel)
            extra["extra_cost"] = pd.to_numeric(extra["extra_cost"], errors="coerce").fillna(0.0)
            extra_agg = extra.groupby("channel", as_index=False).agg(
                extra_cost=("extra_cost", "sum")
            )
            merged = pd.merge(merged, extra_agg, on="channel", how="left").fillna(0)
            merged["ad_spend"] = merged["ad_spend"] + merged["extra_cost"]

    rows: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for _, rec in merged.iterrows():
        channel = str(rec["channel"])
        order_count = int(rec["order_count"])
        gmv = round(float(rec["gmv"]), 2)
        refund_amount = round(float(rec["refund_amount"]), 2)
        net_sales = round(float(rec["net_sales"]), 2)
        impressions = int(rec["impressions"])
        clicks = int(rec["clicks"])
        ad_spend = round(float(rec["ad_spend"]), 2)

        aov = round(net_sales / order_count, 2) if order_count > 0 else 0.0
        refund_rate_pct = round((refund_amount / gmv) * 100, 2) if gmv > 0 else 0.0
        ctr_pct = round((clicks / impressions) * 100, 2) if impressions > 0 else 0.0
        roi = round(net_sales / ad_spend, 2) if ad_spend > 0 else None
        cac = round(ad_spend / order_count, 2) if (ad_spend > 0 and order_count > 0) else 0.0

        prev_val = prev_sales.get(channel)
        dod_pct: float | None = None
        if prev_val is not None and prev_val > 0:
            dod_pct = round(((net_sales - prev_val) / prev_val) * 100, 2)

        reasons: list[str] = []
        if ad_spend > 0 and roi is not None and roi < thresholds.min_roi:
            reasons.append(f"ROI 过低 ({roi:.2f} < {thresholds.min_roi:.2f})")
        if refund_rate_pct > thresholds.max_refund_rate_pct:
            reasons.append(
                f"退款率偏高 ({refund_rate_pct:.1f}% > {thresholds.max_refund_rate_pct:.1f}%)"
            )
        if dod_pct is not None and dod_pct < -thresholds.max_sales_drop_pct:
            reasons.append(
                f"净销售额环比骤降 ({dod_pct:.1f}% < -{thresholds.max_sales_drop_pct:.1f}%)"
            )

        is_anomaly = len(reasons) > 0
        row_dict = {
            "biz_date": biz_date,
            "channel": channel,
            "order_count": order_count,
            "gmv": gmv,
            "refund_amount": refund_amount,
            "net_sales": net_sales,
            "refund_rate_pct": refund_rate_pct,
            "aov": aov,
            "impressions": impressions,
            "clicks": clicks,
            "ctr_pct": ctr_pct,
            "ad_spend": ad_spend,
            "roi": roi if roi is not None else 0.0,
            "has_ad_spend": ad_spend > 0,
            "cac": cac,
            "sales_dod_pct": dod_pct,
            "is_anomaly": is_anomaly,
            "anomaly_reason": "；".join(reasons) if reasons else "正常",
        }
        rows.append(row_dict)
        if is_anomaly:
            anomalies.append(
                {
                    "channel": channel,
                    "net_sales": net_sales,
                    "ad_spend": ad_spend,
                    "roi": roi,
                    "refund_rate_pct": refund_rate_pct,
                    "reason": row_dict["anomaly_reason"],
                }
            )

    channel_df = pd.DataFrame(rows)
    if not channel_df.empty:
        channel_df = channel_df.sort_values(by="net_sales", ascending=False).reset_index(drop=True)

    reconcile_df, reconcile_summary = _reconcile_orders(
        clean_orders, finance_df, thresholds.max_reconcile_diff_yuan
    )

    total_orders = int(channel_df["order_count"].sum()) if not channel_df.empty else 0
    total_gmv = round(float(channel_df["gmv"].sum()), 2) if not channel_df.empty else 0.0
    total_refund = round(float(channel_df["refund_amount"].sum()), 2) if not channel_df.empty else 0.0
    total_net_sales = round(float(channel_df["net_sales"].sum()), 2) if not channel_df.empty else 0.0
    total_spend = round(float(channel_df["ad_spend"].sum()), 2) if not channel_df.empty else 0.0
    overall_roi = round(total_net_sales / total_spend, 2) if total_spend > 0 else 0.0

    kpis = {
        "biz_date": biz_date,
        "total_orders": total_orders,
        "total_gmv": total_gmv,
        "total_refund": total_refund,
        "total_net_sales": total_net_sales,
        "total_ad_spend": total_spend,
        "overall_roi": overall_roi,
        "anomaly_count": len(anomalies),
        "channels": rows,
        "reconcile_summary": reconcile_summary,
        "reconcile_items": reconcile_df.to_dict(orient="records"),
    }

    return EtlResult(
        biz_date=biz_date,
        channel_df=channel_df,
        orders_df=clean_orders,
        kpis=kpis,
        anomalies=anomalies,
        reconcile_df=reconcile_df,
        reconcile_summary=reconcile_summary,
    )
