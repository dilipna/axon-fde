"""Structured logging.

JSON in every environment except local development, where a human-readable
console renderer is used instead. Correlation identifiers are bound to a
context variable so that every log line emitted while handling an incident
carries its ``incident_id`` without it being threaded through call signatures.

Logs never carry secrets, raw prompts or document text. Prompts live in the
LLM trace store behind an access boundary; the log carries a reference.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

__all__ = ["bind_incident", "configure_logging", "get_logger"]

#: Keys whose values are redacted if they ever reach a log event.
_REDACTED_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "authorization",
        "jwt",
        "prompt",
        "document_text",
    }
)

#: Keys that match a sensitive marker but are provenance, not secrets.
#: `prompt_version` and `prompt_hash` are required on every model invocation so
#: a benchmark result can be traced to the exact prompt that produced it
#: (see docs/evaluation/claims.md). Redacting them would break that audit trail
#: to protect nothing: a version string and a hash reveal no prompt content.
_NEVER_REDACT = frozenset(
    {
        "prompt_version",
        "prompt_hash",
        "prompt_id",
        "token_budget",
        "tokens_remaining",
    }
)

_REDACTED = "[redacted]"


def _is_redactable(value: object) -> bool:
    """Whether a value is capable of carrying a secret.

    Booleans, numbers and ``None`` cannot, so redacting them destroys useful
    diagnostics for no security benefit. This distinction matters: the first
    version of this processor turned the boolean ``dev_tokens_allowed`` into
    ``[redacted]`` because the key contains "token", which hid a genuinely
    important security flag behind a false positive.
    """
    return isinstance(value, str | bytes | dict | list | tuple | set)


def _redact_sensitive(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Redact sensitive values as a backstop.

    Correct code never logs these in the first place. This processor exists so
    that a mistake produces a redacted line rather than a leaked credential.
    """
    for key, value in list(event_dict.items()):
        lowered = key.lower()
        if lowered in _NEVER_REDACT or not _is_redactable(value):
            continue
        if any(marker in lowered for marker in _REDACTED_KEYS):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and route the stdlib logger through it."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_sensitive,
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.WriteLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelNamesMapping().get(level.upper(), logging.INFO),
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


@contextmanager
def bind_incident(incident_id: str, **extra: Any) -> Iterator[None]:
    """Bind an incident identifier to every log line inside the block.

    ``incident_id`` is the correlation key that pivots between the UI, the
    trace, the logs and the audit chain, so it is bound once at the edge of
    incident handling rather than passed down through every function.
    """
    tokens = structlog.contextvars.bind_contextvars(incident_id=incident_id, **extra)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
