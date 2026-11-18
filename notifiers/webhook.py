from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any
import httpx

from core.config_loader import NotifierConfig
from core.logger import get_logger
from processors.etl import EtlResult

logger = get_logger("rpa.notifier.webhook")


def compute_feishu_sign(secret: str, timestamp: int) -> str:
    """Compute Feishu custom bot HMAC-SHA256 signature."""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def compute_dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """Compute DingTalk custom bot HMAC-SHA256 URL-encoded signature."""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code))


def _post_webhook(channel: str, payload: dict[str, Any], dry_run: bool, preview_path: Path) -> Path:
    body = dict(payload)
    env_key = "DINGTALK_WEBHOOK_URL" if channel == "dingtalk" else "FEISHU_WEBHOOK_URL"
    webhook_url = os.getenv(env_key, "").strip()

    if channel == "feishu":
        feishu_secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
        if feishu_secret:
            ts = int(time.time())
            body["timestamp"] = str(ts)
            body["sign"] = compute_feishu_sign(feishu_secret, ts)
    elif channel == "dingtalk" and webhook_url:
        dt_secret = os.getenv("DINGTALK_WEBHOOK_SECRET", "").strip()
        if dt_secret:
            ts_ms = int(time.time() * 1000)
            sign = compute_dingtalk_sign(dt_secret, ts_ms)
            sep = "&" if "?" in webhook_url else "?"
            webhook_url = f"{webhook_url}{sep}timestamp={ts_ms}&sign={sign}"

    preview_path.parent.mkdir(parents=True, exist_ok=True)
    with preview_path.open("w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)

    if dry_run or not webhook_url:
        reason = "dry_run=true" if dry_run else f"{env_key} unset"
        logger.info("Webhook notification skipped (%s); preview saved -> %s", reason, preview_path.name)
        return preview_path

    resp = httpx.post(webhook_url, json=body, timeout=10.0)
    resp.raise_for_status()
    logger.info("Sent %s webhook notification (status=%d)", channel, resp.status_code)
    return preview_path


def build_webhook_payload(
    channel: str,
    etl: EtlResult,
    summary_text: str,
    excel_path: Path,
) -> dict[str, Any]:
    k = etl.kpis
    status_tag = "存在异常告警" if k["anomaly_count"] > 0 else "全渠道运行正常"
    if channel == "dingtalk":
        md_text = (
            f"### {etl.biz_date} 经营数据日报 ({status_tag})\n\n"
            f"- 净销售额: **¥{k['total_net_sales']:,.2f}** ({k['total_orders']} 笔)\n"
            f"- 广告消耗: **¥{k['total_ad_spend']:,.2f}** | 整体 ROI: **{k['overall_roi']:.2f}**\n"
            f"- 附件报表: `{excel_path.name}`\n\n"
            f"{summary_text}"
        )
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{etl.biz_date} 经营数据日报",
                "text": md_text,
            },
        }

    # Default: Feishu interactive card payload
    header_template = "red" if k["anomaly_count"] > 0 else "blue"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{etl.biz_date} 跨系统经营日报 | {status_tag}",
                },
                "template": header_template,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**净销售额**: ¥{k['total_net_sales']:,.2f} ({k['total_orders']} 笔)\n"
                        f"**广告消耗**: ¥{k['total_ad_spend']:,.2f} | **整体 ROI**: {k['overall_roi']:.2f}\n"
                        f"**异常渠道**: {k['anomaly_count']} 个\n"
                        f"**产出文件**: `{excel_path.name}`\n\n"
                        f"{summary_text}"
                    ),
                }
            ],
        },
    }


def send_webhook_notification(
    cfg: NotifierConfig,
    etl: EtlResult,
    summary_text: str,
    excel_path: Path,
    output_dir: Path,
    force_dry_run: bool | None = None,
) -> Path:
    """Send Feishu/DingTalk webhook card or save a Dry-Run preview artifact locally."""
    dry_run = cfg.dry_run if force_dry_run is None else force_dry_run
    payload = build_webhook_payload(cfg.channel, etl, summary_text, excel_path)
    preview_path = output_dir / f"notify_preview_{etl.biz_date}.json"
    return _post_webhook(cfg.channel, payload, dry_run=dry_run, preview_path=preview_path)


def send_failure_alert(
    cfg: NotifierConfig,
    job_name: str,
    biz_date: str,
    error_message: str,
    screenshot_path: Path | None,
    output_dir: Path,
    force_dry_run: bool | None = None,
) -> Path:
    """Send an immediate failure alert card (or save preview) when a pipeline run fails."""
    dry_run = cfg.dry_run if force_dry_run is None else force_dry_run
    shot_name = screenshot_path.name if screenshot_path else "无截图"
    remediation = (
        "若提示登录失效，请在本地执行 `python main.py auth-login` 更新登录态并同步至服务器；"
        "若提示元素超时，请前往控制台「失败现场截图」排查页面改版。"
    )
    if cfg.channel == "dingtalk":
        payload: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"[RPA 故障告警] {job_name} ({biz_date})",
                "text": (
                    f"### [RPA 故障告警] {job_name} ({biz_date})\n\n"
                    f"- **错误详情**: {error_message}\n"
                    f"- **现场截图**: `{shot_name}`\n"
                    f"- **排查建议**: {remediation}"
                ),
            },
        }
    else:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"[RPA 故障告警] {job_name} ({biz_date}) 执行失败",
                    },
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**任务名称**: `{job_name}`\n"
                            f"**业务日期**: {biz_date}\n"
                            f"**错误摘要**: {error_message}\n"
                            f"**现场截图**: `{shot_name}`\n\n"
                            f"**排查指引**: {remediation}"
                        ),
                    }
                ],
            },
        }

    preview_path = output_dir / f"alert_preview_{biz_date}.json"
    try:
        return _post_webhook(cfg.channel, payload, dry_run=dry_run, preview_path=preview_path)
    except Exception as exc:
        logger.warning("Failed to send failure alert webhook (preview kept): %s", exc)
        return preview_path
