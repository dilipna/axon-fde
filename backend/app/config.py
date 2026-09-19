"""Application configuration.

Every setting is read from the environment and validated at startup. The
process refuses to start on a malformed or unsafe configuration rather than
failing later, mid-incident, when the cost of a surprise is highest.

Secrets are held as ``SecretStr`` so they do not leak into logs, tracebacks or
``repr()`` output.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Environment", "LLMMode", "Settings", "get_settings"]


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    DEMO = "demo"
    PRODUCTION = "production"


class LLMMode(StrEnum):
    """How the application talks to the language model.

    ``CASSETTE`` replays recorded responses, which is what makes the test suite
    and the demo deterministic, offline and free. It is the default so that an
    accidental run never spends money.
    """

    CASSETTE = "cassette"
    RECORD = "record"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- runtime -----------------------------------------------------------
    axon_env: Environment = Environment.LOCAL
    axon_log_level: str = "INFO"
    axon_api_port: int = Field(default=8000, ge=1, le=65535)

    # -- application database ---------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "axon"
    postgres_password: SecretStr = SecretStr("axon_local_dev")
    postgres_db: str = "axon"

    # -- legacy enterprise system -----------------------------------------
    mssql_host: str = "localhost"
    mssql_port: int = Field(default=1433, ge=1, le=65535)
    mssql_db: str = "AxonERP"
    mssql_sa_password: SecretStr = SecretStr("Axon_Local_Dev_1")
    mssql_ro_user: str = "axon_ai_ro"
    mssql_ro_password: SecretStr = SecretStr("Axon_ReadOnly_Dev_1")
    mssql_driver: str = "ODBC Driver 18 for SQL Server"
    mssql_trust_cert: bool = True
    # Both are defence in depth alongside the read-only grant and the AST
    # allowlist: a query that somehow gets through still cannot run long or
    # return the whole table.
    mssql_query_timeout_seconds: int = Field(default=5, ge=1, le=60)
    mssql_max_rows: int = Field(default=500, ge=1, le=10_000)

    # -- object store ------------------------------------------------------
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_access_key: SecretStr = SecretStr("axonadmin")
    s3_secret_key: SecretStr = SecretStr("axon_local_dev")
    s3_bucket: str = "axon-artifacts"
    s3_region: str = "us-east-1"

    # -- language model ----------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    axon_llm_mode: LLMMode = LLMMode.CASSETTE
    axon_model_reasoning: str = "claude-opus-5"
    axon_model_extraction: str = "claude-sonnet-5"
    axon_model_judge: str = "claude-haiku-4-5"
    # A careless benchmark loop can otherwise burn a month's budget in an hour.
    axon_daily_spend_limit_usd: float = Field(default=5.0, ge=0.0)

    # -- agent budgets -----------------------------------------------------
    # Exceeding any budget escalates the incident to a human rather than
    # letting the workflow spin. Each is enforced independently.
    axon_max_steps: int = Field(default=25, ge=1)
    axon_max_tool_calls: int = Field(default=40, ge=1)
    axon_max_wall_clock_seconds: int = Field(default=180, ge=1)
    axon_max_tokens_per_incident: int = Field(default=120_000, ge=1000)

    # -- authentication ----------------------------------------------------
    axon_jwt_secret: SecretStr = SecretStr("dev-only-not-a-real-secret-change-me")
    axon_jwt_issuer: str = "axonfde-local"
    axon_jwt_audience: str = "axonfde-api"
    axon_jwt_ttl_seconds: int = Field(default=3600, ge=60)

    # -- observability -----------------------------------------------------
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "axonfde-api"

    # -- streaming (Phase 2) ----------------------------------------------
    redpanda_brokers: str = "localhost:9092"

    # -- simulation --------------------------------------------------------
    axon_scenario_pack: str = "pack_v1"
    axon_default_seed: int = 20260918

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    # Deliberately a plain property rather than a computed_field: a
    # computed_field is included in repr() and model_dump(), which would put
    # the database password into any serialization or debug output of Settings.
    # The same reasoning applies to mssql_odbc_dsn() being a method.
    @property
    def postgres_dsn(self) -> str:
        """Async SQLAlchemy DSN for the application database."""
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def mssql_odbc_dsn(self, *, readonly: bool = True) -> str:
        """ODBC connection string for the legacy system.

        ``readonly=True`` uses the restricted ``axon_ai_ro`` login, which holds
        SELECT on six views and nothing else. The ``sa`` login is reachable
        only with ``readonly=False``, which the seed script alone uses.
        """
        user = self.mssql_ro_user if readonly else "sa"
        password = (
            self.mssql_ro_password if readonly else self.mssql_sa_password
        ).get_secret_value()
        trust = "yes" if self.mssql_trust_cert else "no"
        return (
            f"DRIVER={{{self.mssql_driver}}};"
            f"SERVER={self.mssql_host},{self.mssql_port};"
            f"DATABASE={self.mssql_db};"
            f"UID={user};PWD={password};"
            f"TrustServerCertificate={trust};"
            f"Connection Timeout={self.mssql_query_timeout_seconds}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allow_dev_tokens(self) -> bool:
        """Whether the development token endpoint may be served.

        Hard-gated on the local environment. A dev token issuer reachable from
        a deployed environment would be a complete authentication bypass.
        """
        return self.axon_env is Environment.LOCAL

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_calls_cost_money(self) -> bool:
        return self.axon_llm_mode in (LLMMode.RECORD, LLMMode.LIVE)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracing_enabled(self) -> bool:
        return self.langfuse_public_key is not None and self.langfuse_secret_key is not None

    # ------------------------------------------------------------------
    # Safety validation
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _reject_unsafe_deployed_configuration(self) -> Self:
        """Refuse to start a deployed environment with development defaults.

        These are the mistakes that are easy to make and expensive to discover
        in production, so they are fatal at startup rather than advisory.
        """
        if self.axon_env in (Environment.LOCAL, Environment.CI):
            return self

        problems: list[str] = []

        if "dev-only" in self.axon_jwt_secret.get_secret_value():
            problems.append("AXON_JWT_SECRET is still the development default")
        if len(self.axon_jwt_secret.get_secret_value()) < 32:
            problems.append("AXON_JWT_SECRET is shorter than 32 characters")
        if "local_dev" in self.postgres_password.get_secret_value():
            problems.append("POSTGRES_PASSWORD is still the development default")
        if "Local_Dev" in self.mssql_sa_password.get_secret_value():
            problems.append("MSSQL_SA_PASSWORD is still the development default")
        if self.llm_calls_cost_money and self.anthropic_api_key is None:
            problems.append("LLM mode requires an API key but ANTHROPIC_API_KEY is unset")

        if problems:
            raise ValueError(
                f"Unsafe configuration for AXON_ENV={self.axon_env}:\n  - "
                + "\n  - ".join(problems)
            )
        return self

    @model_validator(mode="after")
    def _warn_on_spend_risk(self) -> Self:
        """Make a money-spending configuration impossible to set by accident."""
        if self.llm_calls_cost_money and self.axon_daily_spend_limit_usd <= 0:
            raise ValueError(
                f"AXON_LLM_MODE={self.axon_llm_mode} will call a paid API but "
                "AXON_DAILY_SPEND_LIMIT_USD is 0. Set a positive ceiling."
            )
        if self.llm_calls_cost_money and self.anthropic_api_key is None:
            # Not fatal locally: this is usually someone forgetting to export
            # the key, and a clear message beats a confusing 401 later.
            print(  # noqa: T201 - startup diagnostics predate logging setup
                f"WARNING: AXON_LLM_MODE={self.axon_llm_mode} requires "
                "ANTHROPIC_API_KEY, which is not set. Falling back to cassette "
                "mode would be silent, so calls will fail instead.",
                file=sys.stderr,
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that validation runs once and every component observes the same
    configuration. Tests override via ``get_settings.cache_clear()``.
    """
    return Settings()
