"""Tests for the log redaction backstop.

Redaction has two failure modes and both matter. Under-redacting leaks a
credential. Over-redacting destroys the diagnostics an operator needs, which is
how a security control quietly becomes an obstacle and gets removed.
"""

from __future__ import annotations

import pytest

from backend.app.observability.logging import _redact_sensitive

pytestmark = pytest.mark.unit


def redact(**event: object) -> dict[str, object]:
    return dict(_redact_sensitive(None, "info", dict(event)))


# ---------------------------------------------------------------------------
# Secrets are removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "postgres_password",
        "secret",
        "jwt_secret",
        "api_key",
        "anthropic_api_key",
        "authorization",
        "token",
        "access_token",
        "prompt",
        "system_prompt",
        "document_text",
    ],
)
def test_string_secrets_are_redacted(key: str):
    assert redact(**{key: "super-secret-value"})[key] == "[redacted]"


def test_structured_values_under_a_sensitive_key_are_redacted():
    """A dict or list can carry a secret just as easily as a string."""
    assert redact(credentials_token={"value": "abc"})["credentials_token"] == "[redacted]"
    assert redact(secret_list=["a", "b"])["secret_list"] == "[redacted]"


# ---------------------------------------------------------------------------
# Diagnostics are preserved
# ---------------------------------------------------------------------------


def test_boolean_flags_are_not_redacted():
    """Regression: `dev_tokens_allowed` was redacted because its name contains
    "token", hiding an important security flag behind a false positive.

    A boolean cannot carry a secret, so redacting it costs information and
    buys nothing.
    """
    assert redact(dev_tokens_allowed=True)["dev_tokens_allowed"] is True
    assert redact(tracing_enabled=False)["tracing_enabled"] is False


def test_numeric_values_are_not_redacted():
    """Token *counts* are cost telemetry, not credentials."""
    assert redact(input_tokens=1523)["input_tokens"] == 1523
    assert redact(prompt_tokens=42)["prompt_tokens"] == 42
    assert redact(cache_read_input_tokens=0)["cache_read_input_tokens"] == 0


def test_none_is_not_redacted():
    """`api_key=None` is a meaningful diagnostic: the key is unset."""
    assert redact(api_key=None)["api_key"] is None


def test_ordinary_fields_pass_through_untouched():
    event = redact(
        event="axonfde.startup",
        incident_id="INC-001",
        environment="local",
        observation_types=18,
    )
    assert event["event"] == "axonfde.startup"
    assert event["incident_id"] == "INC-001"
    assert event["environment"] == "local"
    assert event["observation_types"] == 18


def test_prompt_version_is_preserved_but_prompt_text_is_not():
    """Prompt *version* is required provenance; prompt *text* is not logged.

    Every claim in the register must trace to the exact prompt that produced
    it, so `prompt_version` and `prompt_hash` are explicitly allowlisted. They
    reveal no prompt content. The prompt itself belongs in the trace store
    behind an access boundary, never in a log line.
    """
    event = redact(
        prompt_version="v3",
        prompt_hash="9f2c1a",
        prompt="You are a dispatcher assistant",
    )
    assert event["prompt_version"] == "v3"
    assert event["prompt_hash"] == "9f2c1a"
    assert event["prompt"] == "[redacted]"


def test_allowlist_does_not_leak_actual_secrets():
    """The allowlist is exact-match only, so it cannot be widened by accident."""
    assert redact(prompt_version_secret="abc")["prompt_version_secret"] == "[redacted]"
    assert redact(prompt_versions="abc")["prompt_versions"] == "[redacted]"
