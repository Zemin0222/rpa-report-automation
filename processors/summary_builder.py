from __future__ import annotations

import os
from typing import Any
from openai import OpenAI

from core.config_loader import LlmConfig
from core.logger import get_logger
from processors.etl import EtlResult

logger = get_logger("rpa.summary")


def build_rule_summary(etl: EtlResult) -> str:
    """Generate a deterministic, factual Chinese operations summary from ETL metrics."""
    k = etl.kpis
    lines = [
        f"【{etl.biz_date} 经营日报摘要】",
        (
            f"全渠道共成交 {k['total_orders']} 笔订单，GMV ¥{k['total_gmv']:,.2f}，"
            f"退款 ¥{k['total_refund']:,.2f}，净销售额 ¥{k['total_net_sales']:,.2f}；"
            f"广告总消耗 ¥{k['total_ad_spend']:,.2f}，整体投产比 ROI 为 {k['overall_roi']:.2f}。"
        ),
    ]

    if not etl.channel_df.empty:
        top_row = etl.channel_df.iloc[0]
        lines.append(
            f"主力贡献渠道为「{top_row['channel']}」，净销售额 ¥{float(top_row['net_sales']):,.2f}，"
            f"占全盘 {((float(top_row['net_sales']) / max(k['total_net_sales'], 1.0)) * 100):.1f}%。"
        )

    if etl.anomalies:
        lines.append(f"共监测到 {len(etl.anomalies)} 个异常渠道需重点关注：")
        for item in etl.anomalies:
            lines.append(f"- {item['channel']}：{item['reason']}")
    else:
        lines.append("各渠道核心指标均处于阈值安全区间，无异常告警。")

    rec = etl.reconcile_summary
    if rec.get("enabled"):
        lines.append(
            f"【CRM-财务自动对账】共核对 {rec['total_checked']} 笔订单，完全匹配 {rec['matched_count']} 笔，"
            f"发现差异 {rec['discrepancy_count']} 笔（累计差异绝对额 ¥{rec['total_diff_amount']:,.2f}）。"
        )

    return "\n".join(lines)


def build_summary(
    etl: EtlResult,
    llm_cfg: LlmConfig,
    gateway: object | None = None,
) -> str:
    """Optionally enrich the rule summary via AiGateway with cache, budget guard, and safe fallback."""
    base_summary = build_rule_summary(etl)
    from ai.gateway import AiGateway

    gw = gateway if isinstance(gateway, AiGateway) else AiGateway(llm_cfg)
    if not llm_cfg.enabled and gw.injected_client is None:
        return base_summary

    res = gw.generate_daily_advice(base_summary)
    advice = res.data
    bullets: list[str] = []
    if advice.highlights:
        bullets.append("核心亮点：" + "；".join(advice.highlights))
    if advice.suggestions:
        for idx, item in enumerate(advice.suggestions, start=1):
            bullets.append(f"{idx}. {item}")

    if bullets:
        tag = "AI 缓存" if res.source == "cache" else "AI 运营建议"
        return f"{base_summary}\n\n【{tag}】\n" + "\n".join(bullets)
    return base_summary
