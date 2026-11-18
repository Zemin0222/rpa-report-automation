from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ScheduleConfig(BaseModel):
    daily_cron: str = "30 8 * * *"
    weekly_cron: str = "0 9 * * 1"


class BrowserConfig(BaseModel):
    headless: bool = True
    timeout_ms: int = 15000
    slow_mo_ms: int = 0


class WebConnectorConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8765"
    username_env: str = ""
    password_env: str = ""
    default_username: str = ""
    default_password: str = ""

    def resolve_credentials(self) -> tuple[str, str]:
        user = os.getenv(self.username_env, "").strip() or self.default_username
        pwd = os.getenv(self.password_env, "").strip() or self.default_password
        return user, pwd


class LocalDropConfig(BaseModel):
    enabled: bool = True
    drop_dir: str = "storage/dropzone"


class ConnectorsConfig(BaseModel):
    mock_crm: WebConnectorConfig = Field(default_factory=WebConnectorConfig)
    mock_ads: WebConnectorConfig = Field(default_factory=WebConnectorConfig)
    mock_finance: WebConnectorConfig = Field(default_factory=WebConnectorConfig)
    mock_oa: WebConnectorConfig = Field(default_factory=WebConnectorConfig)
    mock_expense: WebConnectorConfig = Field(default_factory=WebConnectorConfig)
    local_drop: LocalDropConfig = Field(default_factory=LocalDropConfig)


class ExpenseConfig(BaseModel):
    company_name: str = "星辰智联科技有限公司"
    company_tax_id: str = "91310115MA1K88990X"
    applicant_name: str = "林小默"
    department: str = "运营交付中心"
    max_invoice_age_days: int = 30
    auto_submit_flagged: bool = False
    category_limits: dict[str, float] = Field(
        default_factory=lambda: {
            "餐饮": 200.0,
            "交通": 300.0,
            "住宿": 500.0,
            "办公": 1000.0,
            "通信": 300.0,
        }
    )


class SourceSpec(BaseModel):
    id: str
    connector: str
    role: Literal["orders", "ads", "finance", "extra_cost"] = "extra_cost"
    enabled: bool = True
    column_mapping: dict[str, str] = Field(default_factory=dict)
    archive_after_read: bool = False
    deduplicate: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class JobDefinition(BaseModel):
    job_name: str = "daily_ops_report"
    workflow: str = "daily_ops_report"
    title: str = ""
    description: str = ""
    enabled: bool = True
    daily_cron: str = "30 8 * * *"
    weekly_cron: str = "0 9 * * 1"
    sources: list[SourceSpec] = Field(default_factory=list)


class ThresholdConfig(BaseModel):
    min_roi: float = 2.0
    max_sales_drop_pct: float = 20.0
    max_refund_rate_pct: float = 15.0
    max_reconcile_diff_yuan: float = 1.0


class NotifierConfig(BaseModel):
    dry_run: bool = True
    channel: Literal["feishu", "dingtalk"] = "feishu"
    email_enabled: bool = False


class LlmConfig(BaseModel):
    enabled: bool = False
    model: str = "gpt-4o-mini"
    daily_call_budget: int = 60
    daily_token_budget: int = 80000
    cache_enabled: bool = True
    max_retries: int = 1


class RetentionConfig(BaseModel):
    downloads_days: int = 14
    screenshots_days: int = 14
    outputs_days: int = 90
    logs_days: int = 30


class StoragePaths(BaseModel):
    root: Path
    db_path: Path
    auth_states: Path
    downloads: Path
    outputs: Path
    screenshots: Path
    dropzone: Path
    logs: Path
    backups: Path | None = None
    live_frames: Path | None = None
    invoices: Path | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.backups is None:
            self.backups = self.root / "backups"
        if self.live_frames is None:
            self.live_frames = self.root / "live_frames"
        if self.invoices is None:
            self.invoices = self.root / "invoices"

    def ensure_all(self) -> None:
        assert self.backups is not None
        assert self.live_frames is not None
        assert self.invoices is not None
        for path in (
            self.root,
            self.auth_states,
            self.downloads,
            self.outputs,
            self.screenshots,
            self.dropzone,
            self.logs,
            self.backups,
            self.live_frames,
            self.invoices,
        ):
            path.mkdir(parents=True, exist_ok=True)


class AppConfig(BaseModel):
    job_name: str = "daily_ops_report"
    timezone: str = "Asia/Shanghai"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    jobs: list[JobDefinition] = Field(default_factory=list)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    expense: ExpenseConfig = Field(default_factory=ExpenseConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    storage: StoragePaths

    def get_job(self, job_name: str | None = None) -> JobDefinition:
        target = job_name or self.job_name
        for job in self.jobs:
            if job.job_name == target:
                return job
        if job_name and job_name != self.job_name:
            raise ValueError(f"Unknown job_name '{job_name}' in configuration")

        # Legacy fallback synthesized from top-level connectors
        return JobDefinition(
            job_name=self.job_name,
            enabled=True,
            daily_cron=self.schedule.daily_cron,
            weekly_cron=self.schedule.weekly_cron,
            sources=[
                SourceSpec(
                    id="mock_crm",
                    connector="mock_crm",
                    role="orders",
                    enabled=self.connectors.mock_crm.enabled,
                ),
                SourceSpec(
                    id="mock_ads",
                    connector="mock_ads",
                    role="ads",
                    enabled=self.connectors.mock_ads.enabled,
                ),
                SourceSpec(
                    id="mock_finance",
                    connector="mock_finance",
                    role="finance",
                    enabled=self.connectors.mock_finance.enabled,
                ),
                SourceSpec(
                    id="local_drop",
                    connector="local_drop",
                    role="extra_cost",
                    enabled=self.connectors.local_drop.enabled,
                ),
            ],
        )


def _parse_bool_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load YAML configuration, overlay .env and runtime environment overrides."""
    env_file = PROJECT_ROOT / "config" / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    yaml_path = config_path or (PROJECT_ROOT / "config" / "jobs.yaml")
    raw_data: dict = {}
    if yaml_path.exists():
        with yaml_path.open("r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f) or {}

    storage_root = PROJECT_ROOT / "storage"
    drop_rel = (
        raw_data.get("connectors", {})
        .get("local_drop", {})
        .get("drop_dir", "storage/dropzone")
    )
    drop_path = Path(drop_rel)
    if not drop_path.is_absolute():
        drop_path = PROJECT_ROOT / drop_path

    storage_paths = StoragePaths(
        root=storage_root,
        db_path=storage_root / "state.db",
        auth_states=storage_root / "auth_states",
        downloads=storage_root / "downloads",
        outputs=storage_root / "outputs",
        screenshots=storage_root / "screenshots",
        dropzone=drop_path,
        logs=storage_root / "logs",
        backups=storage_root / "backups",
        live_frames=storage_root / "live_frames",
        invoices=storage_root / "invoices",
    )
    storage_paths.ensure_all()

    cfg = AppConfig(**raw_data, storage=storage_paths)

    headless_override = _parse_bool_env("RPA_HEADLESS")
    if headless_override is not None:
        cfg.browser.headless = headless_override

    mock_base_url = os.getenv("MOCK_BASE_URL", "").strip()
    if mock_base_url:
        cfg.connectors.mock_crm.base_url = mock_base_url.rstrip("/")
        cfg.connectors.mock_ads.base_url = mock_base_url.rstrip("/")
        cfg.connectors.mock_finance.base_url = mock_base_url.rstrip("/")
        cfg.connectors.mock_oa.base_url = mock_base_url.rstrip("/")
        cfg.connectors.mock_expense.base_url = mock_base_url.rstrip("/")

    return cfg
