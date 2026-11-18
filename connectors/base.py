from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import pandas as pd


@dataclass
class CollectResult:
    source_name: str
    biz_date: str
    dataframe: pd.DataFrame
    raw_file_path: Path | None = None
    row_count: int = 0

    def __post_init__(self) -> None:
        if self.row_count == 0 and not self.dataframe.empty:
            self.row_count = len(self.dataframe)


class BaseConnector(ABC):
    """Abstract interface for all RPA data source connectors."""

    name: str = "base"

    @abstractmethod
    def fetch(self, biz_date: str) -> CollectResult:
        """Collect data for a given business date (YYYY-MM-DD)."""
        raise NotImplementedError
