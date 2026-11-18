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


def build_summary(etl: EtlResult, llm_cfg: LlmConfig) -> str:
    """Optionally enrich the rule summary via OpenAI-compatible LLM with safe fallback."""
    base_summary = build_rule_summary(etl)
    if not llm_cfg.enabled:
        return base_summary

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.info("LLM summary enabled in config, but OPENAI_API_KEY is unset; using rule summary.")
        return base_summary

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.getenv("OPENAI_MODEL", "").strip() or llm_cfg.model

    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)
        prompt = (
            "你是资深运营数据分析师。请基于以下日报事实数据，用简洁专业的中文补充 2 条可执行的运营优化建议"
            "（严禁编造未出现的数字，总字数控制在 180 字以内）：\n\n"
            f"{base_summary}"
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        ai_note = (resp.choices[0].message.content or "").strip()
        if ai_note:
            return f"{base_summary}\n\n【AI 运营建议】\n{ai_note}"
    except Exception as exc:
        logger.warning("LLM summary call failed, falling back to rule summary: %s", exc)

    return base_summary
