"""Tests for configuration validation.

Configuration is the layer where a quiet mistake becomes an outage or a
credential leak, so the failures below are all deliberately fatal at startup.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.config import Environment, LLMMode, Settings

pytestmark = pytest.mark.unit


def make_settings(**overrides: object) -> Settings:
    """Build settings without reading a developer's local .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Safe defaults
# ---------------------------------------------------------------------------


def test_defaults_are_local_and_cost_nothing():
    """An accidental run must never spend money."""
    settings = make_settings()
    assert settings.axon_env is Environment.LOCAL
    assert settings.axon_llm_mode is LLMMode.CASSETTE
    assert settings.llm_calls_cost_money is False


def test_reasoning_model_is_the_frontier_model():
    settings = make_settings()
    assert settings.axon_model_reasoning == "claude-opus-5"


def test_tracing_is_off_until_both_keys_are_present():
    assert make_settings().tracing_enabled is False
    assert make_settings(langfuse_public_key="pk", langfuse_secret_key="sk").tracing_enabled is True
    # One key alone is a misconfiguration, not partial tracing.
    assert make_settings(langfuse_public_key="pk").tracing_enabled is False


# ---------------------------------------------------------------------------
# Secrets do not leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["postgres_password", "mssql_sa_password", "mssql_ro_password", "axon_jwt_secret"],
)
def test_secrets_are_not_exposed_in_repr(field: str):
    settings = make_settings()
    assert "axon_local_dev" not in repr(getattr(settings, field))
    assert "**" in repr(getattr(settings, field))


def test_full_settings_repr_contains_no_secret_values():
    rendered = repr(make_settings())
    for leaked in ("axon_local_dev", "Axon_Local_Dev_1", "dev-only-not-a-real-secret"):
        assert leaked not in rendered


# ---------------------------------------------------------------------------
# The dev token issuer is hard-gated
# ---------------------------------------------------------------------------


def test_dev_tokens_allowed_only_in_local():
    """A dev token issuer reachable from a deployed environment is a full
    authentication bypass, so this gate is on the environment, not a flag."""
    assert make_settings(axon_env="local").allow_dev_tokens is True
    for env in ("ci", "demo", "production"):
        settings = make_settings(
            axon_env=env,
            axon_jwt_secret="x" * 40,
            postgres_password="a-real-password",
            mssql_sa_password="A-Real-Password-1",
        )
        assert settings.allow_dev_tokens is False, env


# ---------------------------------------------------------------------------
# Deployed environments reject development defaults
# ---------------------------------------------------------------------------


def _deployed(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "axon_env": "production",
        "axon_jwt_secret": "x" * 40,
        "postgres_password": "a-real-password",
        "mssql_sa_password": "A-Real-Password-1",
    }
    base.update(overrides)
    return base


def test_production_accepts_a_properly_configured_environment():
    settings = make_settings(**_deployed())
    assert settings.axon_env is Environment.PRODUCTION


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"axon_jwt_secret": "dev-only-not-a-real-secret-change-me"}, "JWT_SECRET"),
        ({"axon_jwt_secret": "too-short"}, "32 characters"),
        ({"postgres_password": "axon_local_dev"}, "POSTGRES_PASSWORD"),
        ({"mssql_sa_password": "Axon_Local_Dev_1"}, "MSSQL_SA_PASSWORD"),
    ],
)
def test_production_rejects_development_defaults(override, expected):
    with pytest.raises(ValidationError, match=expected):
        make_settings(**_deployed(**override))


def test_production_rejects_paid_llm_mode_without_a_key():
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        make_settings(**_deployed(axon_llm_mode="live", anthropic_api_key=None))


def test_local_does_not_enforce_deployment_hardening():
    """Local development must stay frictionless; the gate is environment-scoped."""
    settings = make_settings(axon_env="local")
    assert "dev-only" in settings.axon_jwt_secret.get_secret_value()


# ---------------------------------------------------------------------------
# Spend protection
# ---------------------------------------------------------------------------


def test_paid_mode_requires_a_positive_spend_ceiling():
    with pytest.raises(ValidationError, match="positive ceiling"):
        make_settings(
            axon_llm_mode="live",
            anthropic_api_key="sk-test",
            axon_daily_spend_limit_usd=0,
        )


def test_cassette_mode_needs_no_ceiling():
    settings = make_settings(axon_llm_mode="cassette", axon_daily_spend_limit_usd=0)
    assert settings.llm_calls_cost_money is False


# ---------------------------------------------------------------------------
# Legacy database access
# ---------------------------------------------------------------------------


def test_legacy_dsn_defaults_to_the_restricted_login():
    """Read-only is the default; reaching sa must be an explicit act."""
    settings = make_settings()
    assert "UID=axon_ai_ro" in settings.mssql_odbc_dsn()
    assert "UID=sa" in settings.mssql_odbc_dsn(readonly=False)


def test_legacy_dsn_carries_a_connection_timeout():
    assert "Connection Timeout=5" in make_settings().mssql_odbc_dsn()


def test_query_timeout_and_row_cap_are_bounded():
    """Defence in depth alongside the grant and the AST allowlist."""
    with pytest.raises(ValidationError):
        make_settings(mssql_query_timeout_seconds=0)
    with pytest.raises(ValidationError):
        make_settings(mssql_max_rows=0)


def test_postgres_dsn_uses_the_async_driver():
    dsn = make_settings().postgres_dsn
    assert dsn.startswith("postgresql+asyncpg://")
    assert "axon" in dsn


# ---------------------------------------------------------------------------
# Agent budgets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "axon_max_steps",
        "axon_max_tool_calls",
        "axon_max_wall_clock_seconds",
    ],
)
def test_agent_budgets_must_be_positive(field: str):
    """A zero budget would abort every incident before it began."""
    with pytest.raises(ValidationError):
        make_settings(**{field: 0})
