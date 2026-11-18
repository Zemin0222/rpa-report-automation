from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel

from ai.schemas import (
    ChannelAnomalyExplanation,
    DailyExecutiveAdvice,
    InvoiceExtractionSchema,
    ReconcileAttribution,
)
from core.config_loader import LlmConfig
from core.logger import get_logger
from storage.state_db import StateDB

logger = get_logger("rpa.ai.gateway")

T = TypeVar("T", bound=BaseModel)
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass
class AiResult(Generic[T]):
    """Envelope returned by AiGateway preserving provenance, latency, and token telemetry."""

    data: T
    source: Literal["ai", "cache", "rule"]
    model: str
    latency_ms: float = 0.0
    tokens: int = 0
    fallback_reason: str = ""


def load_prompt(name: str, **variables: Any) -> str:
    """Load a Markdown prompt template under ai/prompts/ and substitute keyword variables."""
    path = PROMPTS_DIR / f"{name}.md"
    template = path.read_text(encoding="utf-8")
    return template.format(**variables)


def _extract_json_object(raw_text: str) -> Any:
    cleaned = (raw_text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    else:
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            cleaned = brace.group(0).strip()
    return json.loads(cleaned)


class AiGateway:
    """Unified OpenAI-compatible AI gateway with SQLite cache, daily budget cap, schema retry, and rule fallback."""

    def __init__(
        self,
        cfg: LlmConfig,
        db: StateDB | None = None,
        client: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.injected_client = client

    def resolve_model(self) -> str:
        return os.getenv("OPENAI_MODEL", "").strip() or self.cfg.model

    def resolve_api_key(self) -> str:
        return os.getenv("OPENAI_API_KEY", "").strip()

    def resolve_base_url(self) -> str:
        return os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()

    def _get_client(self) -> Any:
        if self.injected_client is not None:
            return self.injected_client
        from openai import OpenAI

        return OpenAI(
            api_key=self.resolve_api_key(),
            base_url=self.resolve_base_url(),
            timeout=25.0,
        )

    @staticmethod
    def _compute_cache_key(
        task: str,
        model: str,
        prompt: str,
        images: list[bytes] | None = None,
    ) -> str:
        h = hashlib.sha256()
        h.update(task.encode("utf-8"))
        h.update(b"|")
        h.update(model.encode("utf-8"))
        h.update(b"|")
        h.update(prompt.encode("utf-8"))
        for img in images or []:
            h.update(b"|")
            h.update(hashlib.sha256(img).digest())
        return h.hexdigest()

    def _record_usage(
        self,
        task: str,
        source: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        if self.db is None:
            return
        try:
            self.db.record_ai_usage(
                usage_date=date.today().isoformat(),
                task=task,
                source=source,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            logger.warning("Failed to persist AI usage telemetry: %s", exc)

    def complete_json(
        self,
        task: str,
        prompt: str,
        schema: type[T],
        fallback_factory: Callable[[str], T],
        images: list[bytes] | None = None,
    ) -> AiResult[T]:
        """Execute a schema-verified JSON completion with cache, daily budget guard, retry, and fallback."""
        model = self.resolve_model()
        t0 = time.perf_counter()
        cache_key = self._compute_cache_key(task, model, prompt, images=images)

        # 1. SQLite semantic-hash cache check
        if self.cfg.cache_enabled and self.db is not None:
            cached = self.db.get_ai_cache(cache_key)
            if cached is not None:
                try:
                    parsed = schema.model_validate_json(cached["response_json"])
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    self._record_usage(
                        task=task,
                        source="cache",
                        model=cached["model"],
                        total_tokens=int(cached.get("tokens_used") or 0),
                        latency_ms=elapsed_ms,
                    )
                    return AiResult(
                        data=parsed,
                        source="cache",
                        model=cached["model"],
                        latency_ms=round(elapsed_ms, 1),
                        tokens=int(cached.get("tokens_used") or 0),
                    )
                except Exception:
                    pass

        # 2. Check enablement and credentials
        has_client_or_key = bool(self.injected_client is not None or self.resolve_api_key())
        if not self.cfg.enabled or not has_client_or_key:
            reason = "llm_disabled" if not self.cfg.enabled else "api_key_unset"
            data = fallback_factory(reason)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self._record_usage(task=task, source="rule", model="rule-engine", latency_ms=elapsed_ms)
            return AiResult(
                data=data,
                source="rule",
                model="rule-engine",
                latency_ms=round(elapsed_ms, 1),
                fallback_reason=reason,
            )

        # 3. Check daily call & token budgets
        if self.db is not None:
            stats = self.db.get_ai_daily_stats(date.today().isoformat())
            if stats["calls_today"] >= self.cfg.daily_call_budget:
                data = fallback_factory("daily_call_budget_exceeded")
                elapsed_ms = (time.perf_counter() - t0) * 1000
                self._record_usage(task=task, source="rule", model="rule-engine", latency_ms=elapsed_ms)
                return AiResult(
                    data=data,
                    source="rule",
                    model="rule-engine",
                    latency_ms=round(elapsed_ms, 1),
                    fallback_reason="daily_call_budget_exceeded",
                )
            if stats["tokens_today"] >= self.cfg.daily_token_budget:
                data = fallback_factory("daily_token_budget_exceeded")
                elapsed_ms = (time.perf_counter() - t0) * 1000
                self._record_usage(task=task, source="rule", model="rule-engine", latency_ms=elapsed_ms)
                return AiResult(
                    data=data,
                    source="rule",
                    model="rule-engine",
                    latency_ms=round(elapsed_ms, 1),
                    fallback_reason="daily_token_budget_exceeded",
                )

        # 4. Call OpenAI-compatible model with schema validation retry
        user_content: Any
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for img in images:
                b64 = base64.b64encode(img).decode("ascii")
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
            user_content = parts
        else:
            user_content = prompt

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        attempts = max(1, int(self.cfg.max_retries) + 1)
        last_err = ""
        total_prompt_tok = 0
        total_comp_tok = 0
        total_tok = 0

        try:
            client = self._get_client()
            for attempt in range(attempts):
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                )
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    total_prompt_tok += int(getattr(usage, "prompt_tokens", 0) or 0)
                    total_comp_tok += int(getattr(usage, "completion_tokens", 0) or 0)
                    total_tok += int(getattr(usage, "total_tokens", 0) or 0)

                raw_text = (resp.choices[0].message.content or "").strip()
                try:
                    parsed_obj = _extract_json_object(raw_text)
                    validated = schema.model_validate(parsed_obj)
                    elapsed_ms = (time.perf_counter() - t0) * 1000

                    if self.cfg.cache_enabled and self.db is not None:
                        self.db.put_ai_cache(
                            cache_key=cache_key,
                            task=task,
                            model=model,
                            response_json=validated.model_dump_json(),
                            tokens_used=total_tok,
                        )
                    self._record_usage(
                        task=task,
                        source="ai",
                        model=model,
                        prompt_tokens=total_prompt_tok,
                        completion_tokens=total_comp_tok,
                        total_tokens=total_tok,
                        latency_ms=elapsed_ms,
                    )
                    return AiResult(
                        data=validated,
                        source="ai",
                        model=model,
                        latency_ms=round(elapsed_ms, 1),
                        tokens=total_tok,
                    )
                except Exception as val_exc:
                    last_err = str(val_exc)
                    if attempt + 1 < attempts:
                        messages.append({"role": "assistant", "content": raw_text})
                        messages.append(
                            {
                                "role": "user",
                                "content": f"上述 JSON 未通过校验（{last_err}），请修正后仅返回纯 JSON 对象。",
                            }
                        )
        except Exception as api_exc:
            last_err = str(api_exc)

        logger.warning("AI gateway task '%s' fell back to rule engine: %s", task, last_err)
        data = fallback_factory(last_err or "validation_failed")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._record_usage(task=task, source="rule", model="rule-engine", latency_ms=elapsed_ms)
        return AiResult(
            data=data,
            source="rule",
            model="rule-engine",
            latency_ms=round(elapsed_ms, 1),
            fallback_reason=last_err or "validation_failed",
        )

    # ------------------------------------------------------------------
    # High-level Domain Capabilities (AI + Deterministic Rule Fallback)
    # ------------------------------------------------------------------

    def explain_anomaly(
        self,
        channel_row: dict[str, Any],
        trend_context: list[dict[str, Any]] | None = None,
    ) -> AiResult[ChannelAnomalyExplanation]:
        channel = str(channel_row.get("channel", ""))
        roi = float(channel_row.get("roi") or 0.0)
        ad_spend = float(channel_row.get("ad_spend") or 0.0)
        net_sales = float(channel_row.get("net_sales") or 0.0)
        refund_pct = float(channel_row.get("refund_rate_pct") or 0.0)
        dod_pct = channel_row.get("sales_dod_pct")
        reason = str(channel_row.get("anomaly_reason") or "")

        def _fallback(_: str) -> ChannelAnomalyExplanation:
            causes: list[str] = []
            actions: list[str] = []
            urgency: Literal["高", "中", "低"] = "中"

            if ad_spend > 0 and roi < 2.0:
                causes.append(f"投放 ROI 为 {roi:.2f}（低于盈亏线 2.00），¥{ad_spend:,.0f} 预算转化效率偏低")
                actions.append("立即下调高消耗低点击计划出价，暂停 CTR 末位 20% 创意组")
                if roi < 1.0:
                    urgency = "高"
            if refund_pct > 15.0:
                causes.append(f"退款率飙升至 {refund_pct:.1f}%，大量售后冲抵净成交额")
                actions.append("核查近 48 小时商品评价与发货时效，优化落地页预期文案")
                urgency = "高"
            if dod_pct is not None and float(dod_pct) < -20.0:
                causes.append(f"净销售额日环比下滑 {float(dod_pct):.1f}%，流量承接或曝光出现断层")
                actions.append("复查昨日竞品活动分流及核心时段曝光预算分配")

            if not causes:
                causes.append(reason or "触发经营指标预警阈值")
                actions.append("排查近 3 日素材衰退及投放定向人群漂移情况")

            impact = f"当日净销售 ¥{net_sales:,.2f}，投放花费 ¥{ad_spend:,.2f}，建议当日内优先止损。"
            return ChannelAnomalyExplanation(
                channel=channel,
                root_cause="；".join(causes),
                impact=impact,
                recommended_actions=actions,
                urgency=urgency,
            )

        payload_json = json.dumps(
            {
                "channel": channel,
                "metrics": {
                    "net_sales": net_sales,
                    "ad_spend": ad_spend,
                    "roi": roi,
                    "refund_rate_pct": refund_pct,
                    "sales_dod_pct": dod_pct,
                    "trigger_reason": reason,
                },
                "recent_trend": (trend_context or [])[-3:],
            },
            ensure_ascii=False,
        )
        prompt = load_prompt("anomaly_explain", payload_json=payload_json)
        return self.complete_json("anomaly_explain", prompt, ChannelAnomalyExplanation, _fallback)

    def attribute_reconcile_diff(self, item: dict[str, Any]) -> AiResult[ReconcileAttribution]:
        order_id = str(item.get("order_id", ""))
        diff_type = str(item.get("diff_type", ""))
        fin_status = str(item.get("finance_status", ""))
        diff_amt = float(item.get("diff_amount") or 0.0)
        base_advice = str(item.get("action_advice") or "")

        def _fallback(_: str) -> ReconcileAttribution:
            if "漏单" in diff_type:
                causes = [
                    "第三方支付网关异步回调延迟，财务结算批处理尚未入账",
                    "CRM 已确认收单但跨日银行清分切至次日流水",
                ]
                steps = [
                    "核对支付商户号昨日 23:00-24:00 挂账及延迟通知流水",
                    "若 T+1 仍未在财务结算单出现，提交支付渠道补单工单",
                ]
                priority: Literal["P1", "P2", "P3"] = "P1"
            elif "长款" in diff_type:
                causes = [
                    "历史跨月/跨日补结算订单或手工离线收款并入本日财务 PDF",
                    "CRM 侧该笔测试单或异常单未同步回传订单主表",
                ]
                steps = [
                    "按金额反查 CRM 前 7 日待支付订单及人工补录台账",
                    "确认后在财务台账补充业务归属标签以平账",
                ]
                priority = "P2"
            else:
                causes = [
                    f"支付通道扣除了千分之几手续费或营销立减（差额 ¥{abs(diff_amt):.2f}）",
                    f"订单状态为「{fin_status}」，存在部分退款或争议冻结款项",
                ]
                steps = [
                    base_advice or "导出支付渠道分账与优惠券分摊明细核对净实收",
                    "在财务对账单中标记差异类型（手续费差额/部分拒付）",
                ]
                priority = "P1" if fin_status == "DISPUTED" else "P2"

            return ReconcileAttribution(
                order_id=order_id,
                likely_causes=causes,
                check_steps=steps,
                priority=priority,
            )

        prompt = load_prompt("reconcile_attribute", payload_json=json.dumps(item, ensure_ascii=False))
        return self.complete_json("reconcile_attribute", prompt, ReconcileAttribution, _fallback)

    def generate_daily_advice(self, summary_text: str) -> AiResult[DailyExecutiveAdvice]:
        def _fallback(_: str) -> DailyExecutiveAdvice:
            return DailyExecutiveAdvice(highlights=[], suggestions=[])

        prompt = load_prompt("daily_summary", summary_text=summary_text)
        return self.complete_json("daily_summary", prompt, DailyExecutiveAdvice, _fallback)

    def extract_invoice_structured(
        self,
        invoice_text: str,
        fallback_schema: InvoiceExtractionSchema,
        image_bytes: bytes | None = None,
    ) -> AiResult[InvoiceExtractionSchema]:
        def _fallback(_: str) -> InvoiceExtractionSchema:
            return fallback_schema

        prompt = load_prompt("invoice_extract", invoice_text=(invoice_text or "")[:2500])
        images = [image_bytes] if image_bytes else None
        return self.complete_json(
            "invoice_extract",
            prompt,
            InvoiceExtractionSchema,
            _fallback,
            images=images,
        )
