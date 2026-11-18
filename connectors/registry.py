from __future__ import annotations

import importlib
import inspect
from typing import Any
import pandas as pd

from connectors.base import BaseConnector
from connectors.generic_web_connector import GenericWebConnector
from connectors.imap_mail_connector import ImapMailConnector
from connectors.local_drop_connector import LocalDropConnector
from connectors.mock_ads_connector import MockAdsConnector
from connectors.mock_crm_connector import MockCrmConnector
from connectors.mock_finance_connector import MockFinanceConnector
from connectors.template_connector import RealSiteTemplateConnector
from core.browser import BrowserManager
from core.config_loader import AppConfig, SourceSpec
from storage.state_db import StateDB

CONNECTOR_REGISTRY: dict[str, type[BaseConnector]] = {
    "mock_crm": MockCrmConnector,
    "mock_ads": MockAdsConnector,
    "mock_finance": MockFinanceConnector,
    "local_drop": LocalDropConnector,
    "imap_mail": ImapMailConnector,
    "generic_web": GenericWebConnector,
    "real_site_template": RealSiteTemplateConnector,
}


def apply_column_mapping(df: pd.DataFrame, mapping: dict[str, str] | None) -> pd.DataFrame:
    if df is None or not mapping:
        return df
    return df.rename(columns=mapping)


def _resolve_connector_class(name_or_path: str) -> type[BaseConnector]:
    if name_or_path in CONNECTOR_REGISTRY:
        return CONNECTOR_REGISTRY[name_or_path]
    if ":" in name_or_path:
        module_path, class_name = name_or_path.split(":", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        if not issubclass(cls, BaseConnector):
            raise TypeError(f"{name_or_path} is not a subclass of BaseConnector")
        return cls
    raise KeyError(f"Unknown connector '{name_or_path}'. Registered: {sorted(CONNECTOR_REGISTRY)}")


def build_connector(
    source_spec: SourceSpec,
    cfg: AppConfig,
    browser_mgr: BrowserManager | None = None,
    db: StateDB | None = None,
) -> BaseConnector:
    """Instantiate a registered or dynamically imported connector cleanly from SourceSpec."""
    cls = _resolve_connector_class(source_spec.connector)
    sig = inspect.signature(cls.__init__)
    params = sig.parameters

    kwargs: dict[str, Any] = {}
    if "cfg" in params:
        kwargs["cfg"] = cfg
    if "browser_mgr" in params:
        if browser_mgr is None:
            raise ValueError(f"Connector '{source_spec.connector}' requires BrowserManager")
        kwargs["browser_mgr"] = browser_mgr
    if "source_spec" in params:
        kwargs["source_spec"] = source_spec
    if "db" in params:
        kwargs["db"] = db
    if "column_mapping" in params:
        kwargs["column_mapping"] = source_spec.column_mapping
    if "archive_after_read" in params:
        kwargs["archive_after_read"] = source_spec.archive_after_read
    if "deduplicate" in params:
        kwargs["deduplicate"] = source_spec.deduplicate

    return cls(**kwargs)
