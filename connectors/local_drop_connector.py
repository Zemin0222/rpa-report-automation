from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
import pandas as pd
import pdfplumber

from connectors.base import BaseConnector, CollectResult
from core.config_loader import AppConfig
from core.logger import get_logger
from storage.state_db import StateDB

logger = get_logger("rpa.connector.dropzone")


class LocalDropConnector(BaseConnector):
    """Ingests optional offline CSV/Excel/PDF files placed in storage/dropzone."""

    name = "local_drop"

    def __init__(
        self,
        cfg: AppConfig,
        column_mapping: dict[str, str] | None = None,
        archive_after_read: bool = False,
        deduplicate: bool = False,
        db: StateDB | None = None,
    ) -> None:
        self.cfg = cfg
        self.column_mapping = column_mapping or {}
        self.archive_after_read = archive_after_read
        self.deduplicate = deduplicate
        self.db = db

    @staticmethod
    def _read_pdf_tables(pdf_path: Path) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if len(table) >= 2 and table[0]:
                        headers = [str(h or "").strip() for h in table[0]]
                        frames.append(pd.DataFrame(table[1:], columns=headers))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch(self, biz_date: str) -> CollectResult:
        drop_dir = self.cfg.storage.dropzone
        drop_dir.mkdir(parents=True, exist_ok=True)

        candidates = list(drop_dir.glob("*"))
        date_dir = drop_dir / biz_date
        if date_dir.is_dir():
            candidates.extend(date_dir.glob("*"))

        frames: list[pd.DataFrame] = []
        for path in sorted(candidates):
            if not path.is_file() or path.name.startswith("."):
                continue
            suffix = path.suffix.lower()
            if suffix not in {".csv", ".xlsx", ".xls", ".pdf"}:
                continue

            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.deduplicate and self.db and self.db.is_file_processed(file_hash):
                logger.info("Skipping already processed dropzone file: %s", path.name)
                continue

            try:
                if suffix == ".csv":
                    df = pd.read_csv(path, encoding="utf-8-sig")
                elif suffix in {".xlsx", ".xls"}:
                    df = pd.read_excel(path)
                else:
                    df = self._read_pdf_tables(path)

                if self.column_mapping and not df.empty:
                    df = df.rename(columns=self.column_mapping)

                if not df.empty:
                    df["_source_file"] = path.name
                    frames.append(df)
                    if self.deduplicate and self.db:
                        self.db.mark_file_processed(file_hash, self.name, path.name, biz_date)

                if self.archive_after_read:
                    archive_dir = drop_dir / "archive" / biz_date
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    dest = archive_dir / path.name
                    shutil.move(str(path), str(dest))
                    logger.info("Archived dropzone file -> %s", dest)
            except Exception as exc:
                logger.warning("Failed to parse dropzone file %s: %s", path.name, exc)

        merged = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=["biz_date", "channel", "extra_cost"])
        )
        logger.info("LocalDrop collected %d rows from %s", len(merged), drop_dir)
        return CollectResult(
            source_name=self.name,
            biz_date=biz_date,
            dataframe=merged,
            row_count=len(merged),
        )
