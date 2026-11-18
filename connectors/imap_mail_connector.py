from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import os
from pathlib import Path
from typing import Any
import pandas as pd
import pdfplumber

from connectors.base import BaseConnector, CollectResult
from core.config_loader import AppConfig, SourceSpec
from core.logger import get_logger
from storage.state_db import StateDB

logger = get_logger("rpa.connector.imap")

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}


def parse_raw_email_bytes(
    raw_bytes: bytes,
    biz_date: str,
    save_dir: Path,
    subject_keyword: str = "",
    db: StateDB | None = None,
    deduplicate: bool = True,
) -> list[Path]:
    """Parse raw RFC822 email bytes, filter by subject, deduplicate by SHA256, and save attachments."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    subject = str(msg.get("Subject", "") or "")
    if subject_keyword and subject_keyword not in subject:
        return []

    save_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for part in msg.iter_attachments():
        filename = part.get_filename()
        if not filename:
            continue
        clean_name = Path(filename).name
        if Path(clean_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        file_hash = hashlib.sha256(payload).hexdigest()
        if deduplicate and db and db.is_file_processed(file_hash):
            logger.info("Skipping duplicate email attachment: %s", clean_name)
            continue

        target = save_dir / clean_name
        target.write_bytes(payload)
        if db and deduplicate:
            db.mark_file_processed(file_hash, "imap_mail", clean_name, biz_date)
        saved_paths.append(target)

    return saved_paths


class ImapMailConnector(BaseConnector):
    """Fetches CSV/Excel/PDF attachments from an enterprise IMAP mailbox using stdlib imaplib."""

    name = "imap_mail"

    def __init__(
        self,
        cfg: AppConfig,
        source_spec: SourceSpec | None = None,
        db: StateDB | None = None,
    ) -> None:
        self.cfg = cfg
        self.source_spec = source_spec
        self.db = db

    @staticmethod
    def _read_attachment(path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(path, encoding="utf-8-sig")
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(path)
        if suffix == ".pdf":
            frames: list[pd.DataFrame] = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        if len(table) >= 2 and table[0]:
                            headers = [str(h or "").strip() for h in table[0]]
                            frames.append(pd.DataFrame(table[1:], columns=headers))
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return pd.DataFrame()

    def fetch(self, biz_date: str) -> CollectResult:
        host = os.getenv("IMAP_HOST", "").strip()
        port = int(os.getenv("IMAP_PORT", "993") or "993")
        user = os.getenv("IMAP_USER", "").strip()
        password = os.getenv("IMAP_PASSWORD", "").strip()
        folder = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"

        if not (host and user and password):
            logger.info("IMAP credentials unset (IMAP_HOST/IMAP_USER/IMAP_PASSWORD); skipping mail fetch.")
            return CollectResult(
                source_name=self.name,
                biz_date=biz_date,
                dataframe=pd.DataFrame(),
                row_count=0,
            )

        params: dict[str, Any] = self.source_spec.params if self.source_spec else {}
        subject_keyword = str(params.get("subject_keyword", "") or "")
        deduplicate = self.source_spec.deduplicate if self.source_spec else True
        col_map = self.source_spec.column_mapping if self.source_spec else {}
        save_dir = self.cfg.storage.downloads / biz_date / "mail"

        saved_files: list[Path] = []
        with imaplib.IMAP4_SSL(host, port) as client:
            client.login(user, password)
            client.select(folder)
            status, data = client.search(None, "ALL")
            if status == "OK" and data and data[0]:
                msg_ids = data[0].split()[-30:]
                for msg_id in msg_ids:
                    res_status, msg_data = client.fetch(msg_id, "(RFC822)")
                    if res_status != "OK" or not msg_data:
                        continue
                    for item in msg_data:
                        if isinstance(item, tuple) and len(item) >= 2:
                            saved_files.extend(
                                parse_raw_email_bytes(
                                    item[1],
                                    biz_date=biz_date,
                                    save_dir=save_dir,
                                    subject_keyword=subject_keyword,
                                    db=self.db,
                                    deduplicate=deduplicate,
                                )
                            )

        frames: list[pd.DataFrame] = []
        for path in saved_files:
            try:
                df = self._read_attachment(path)
                if col_map and not df.empty:
                    df = df.rename(columns=col_map)
                if not df.empty:
                    df["_source_file"] = path.name
                    frames.append(df)
            except Exception as exc:
                logger.warning("Failed to parse mail attachment %s: %s", path.name, exc)

        merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        logger.info("ImapMail collected %d rows from %d attachments", len(merged), len(saved_files))
        return CollectResult(
            source_name=self.name,
            biz_date=biz_date,
            dataframe=merged,
            row_count=len(merged),
        )
