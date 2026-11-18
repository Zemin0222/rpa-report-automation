from __future__ import annotations

import pytest

from connectors.template_connector import RealSiteTemplateConnector
from core.config_loader import load_config
from main import resolve_auth_login_url


def test_resolve_auth_login_url_presets_and_custom() -> None:
    cfg = load_config()
    assert resolve_auth_login_url(cfg, "mock_crm", None).endswith("/crm/login")
    assert resolve_auth_login_url(cfg, "mock_ads", None).endswith("/ads/login")
    assert resolve_auth_login_url(cfg, "mock_finance", None).endswith("/finance/login")
    assert resolve_auth_login_url(cfg, "my_erp", "https://erp.example.com/login") == "https://erp.example.com/login"

    with pytest.raises(ValueError):
        resolve_auth_login_url(cfg, "unknown_site", None)


def test_template_connector_contract() -> None:
    assert RealSiteTemplateConnector.name == "real_site_template"
    assert hasattr(RealSiteTemplateConnector, "fetch")
