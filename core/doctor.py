from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from datetime import datetime
from typing import Any

from core.config_loader import AppConfig
from storage.state_db import StateDB


def run_doctor_checks(cfg: AppConfig) -> list[dict[str, Any]]:
    """Inspect runtime environment, DB health, disk free space, auth states, and secret presence."""
    checks: list[dict[str, Any]] = []

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    pw_ok = importlib.util.find_spec("playwright") is not None
    checks.append(
        {
            "检查项": "Python 与 Playwright 运行时",
            "状态": "OK" if pw_ok else "WARN",
            "详情": f"Python {py_ver}, playwright={'已安装' if pw_ok else '未安装'}",
        }
    )

    try:
        db = StateDB(cfg.storage.db_path)
        with db._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            cnt = conn.execute("SELECT COUNT(*) FROM job_runs;").fetchone()[0]
        checks.append(
            {
                "检查项": "SQLite 状态库与并发模式",
                "状态": "OK",
                "详情": f"{cfg.storage.db_path.name} (journal_mode={mode}, runs={cnt})",
            }
        )
    except Exception as exc:
        checks.append(
            {
                "检查项": "SQLite 状态库与并发模式",
                "状态": "ERROR",
                "详情": f"连接失败: {exc}",
            }
        )

    usage = shutil.disk_usage(cfg.storage.root)
    free_gb = round(usage.free / (1024**3), 2)
    checks.append(
        {
            "检查项": "存储卷剩余可用空间",
            "状态": "OK" if free_gb >= 1.0 else "WARN",
            "详情": f"{free_gb} GB 可用 ({cfg.storage.root})",
        }
    )

    state_files = sorted(cfg.storage.auth_states.glob("*.json"))
    if state_files:
        details = []
        for sf in state_files:
            age_h = round((datetime.now().timestamp() - sf.stat().st_mtime) / 3600, 1)
            details.append(f"{sf.stem}({age_h}h前)")
        checks.append(
            {
                "检查项": "站点登录态缓存 (auth_states)",
                "状态": "OK",
                "详情": ", ".join(details),
            }
        )
    else:
        checks.append(
            {
                "检查项": "站点登录态缓存 (auth_states)",
                "状态": "WARN",
                "详情": "暂无已保存的登录态 JSON（首次运行将尝试自动登录或需运行 auth-login）",
            }
        )

    secret_keys = [
        ("FEISHU_WEBHOOK_URL", "飞书 Webhook"),
        ("FEISHU_WEBHOOK_SECRET", "飞书加签密钥"),
        ("DINGTALK_WEBHOOK_URL", "钉钉 Webhook"),
        ("DINGTALK_WEBHOOK_SECRET", "钉钉加签密钥"),
        ("SMTP_HOST", "SMTP 发信服务"),
        ("IMAP_HOST", "IMAP 企业收信"),
        ("OPENAI_API_KEY", "大模型摘要 Key"),
        ("DASHBOARD_PASSWORD", "控制台访问口令"),
    ]
    configured = [label for env_key, label in secret_keys if os.getenv(env_key, "").strip()]
    unset = [label for env_key, label in secret_keys if not os.getenv(env_key, "").strip()]
    checks.append(
        {
            "检查项": "外部密钥与通知配置 (脱敏)",
            "状态": "OK" if configured else "INFO",
            "详情": (
                f"已配置: {', '.join(configured) if configured else '无'} | "
                f"未配置(默认/Dry-Run): {', '.join(unset)}"
            ),
        }
    )

    return checks
