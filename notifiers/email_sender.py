from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from core.config_loader import NotifierConfig
from core.logger import get_logger

logger = get_logger("rpa.notifier.email")


def send_email_report(
    cfg: NotifierConfig,
    biz_date: str,
    summary_text: str,
    attachments: list[Path],
    force_dry_run: bool | None = None,
) -> bool:
    """Send report attachments via SMTP SSL when email is enabled and not in dry-run."""
    dry_run = cfg.dry_run if force_dry_run is None else force_dry_run
    if not cfg.email_enabled or dry_run:
        logger.info("Email notification skipped (email_enabled=%s, dry_run=%s)", cfg.email_enabled, dry_run)
        return False

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_to = [addr.strip() for addr in os.getenv("SMTP_TO", "").split(",") if addr.strip()]

    if not (smtp_host and smtp_user and smtp_pass and smtp_to):
        logger.warning("Email enabled but SMTP credentials/recipients incomplete; skipping send.")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[RPA 自动日报] {biz_date} 跨系统经营数据汇总"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(smtp_to)
    msg.set_content(summary_text)

    for path in attachments:
        if path.exists():
            data = path.read_bytes()
            msg.add_attachment(
                data,
                maintype="application",
                subtype="octet-stream",
                filename=path.name,
            )

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    logger.info("Email report sent to %s", ", ".join(smtp_to))
    return True
