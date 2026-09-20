"""Recorded model responses, replayed offline.

Cassettes are what make the test suite and the demo deterministic, free and
network-free. They are also the single most dangerous piece of test
infrastructure in the repository, for one reason:

**A cassette miss must fail loudly. It must never re-record silently.**

A suite that quietly re-records when the prompt changes is a suite that
asserts whatever the model happened to say this morning. Every assertion still
passes, the diff shows only cassette files, and the thing the tests were
protecting stopped being protected without anybody deciding that. So
``CASSETTE`` mode raises on a miss and names the key, the prompt that produced
it and the command that would re-record. Recording happens only in ``RECORD``
mode, which is an explicit act that costs money.

**The key covers what changes the answer.** Model, system prompt, messages,
requested schema, token ceiling and effort - and the prompt version, so that
editing a prompt invalidates its cassettes rather than reusing answers given
to a different question. Anything not in the key is asserted to not matter;
anything in it that does not matter makes cassettes churn. Both are wrong in
different directions, so the covered set is spelled out in ``cassette_key``
rather than left to a dataclass's field order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backend.app.llm.provider import LLMRequest, LLMResponse, TokenUsage

__all__ = [
    "CASSETTE_VERSION",
    "CassetteLibrary",
    "CassetteMissError",
    "cassette_key",
]

#: Bumped when the stored shape changes. Part of the key, so an old cassette
#: is a miss rather than a silently misread file.
CASSETTE_VERSION = 1


class CassetteMissError(RuntimeError):
    """No recording for this request, and recording is not enabled.

    The message is long on purpose. This exception is most often seen by
    somebody who has just edited a prompt and does not yet know that prompts
    and cassettes are coupled, and a terse "cassette not found" sends them
    looking for a missing file rather than at the change they just made.
    """

    def __init__(self, key: str, request: LLMRequest, path: Path) -> None:
        super().__init__(
            f"No cassette for {request.node} ({request.prompt_version}) at key {key}.\n"
            f"Looked in: {path}\n\n"
            "Nothing was re-recorded, deliberately. A suite that re-records on a "
            "miss asserts whatever the model said today, and every test keeps "
            "passing while what they protected quietly stops being protected.\n\n"
            "If the prompt changed, this is expected - re-record it on purpose:\n"
            "    AXON_LLM_MODE=record ANTHROPIC_API_KEY=... uv run pytest <test>\n"
            "That costs money, which is the point."
        )
        self.key = key
        self.request = request
        self.path = path


def cassette_key(request: LLMRequest) -> str:
    """A stable hash of everything that changes the model's answer.

    Written out field by field rather than serialising the dataclass, so that
    adding a field to ``LLMRequest`` is a deliberate decision about whether it
    belongs in the key. A field that silently joined the key would invalidate
    every cassette in the repository; one that silently did not would replay
    an answer to a different question.
    """
    payload = {
        "cassette_version": CASSETTE_VERSION,
        "model": request.model,
        "system": request.system,
        "messages": list(request.messages),
        "max_tokens": request.max_tokens,
        "output_schema": request.output_schema,
        "effort": request.effort,
        # In the key so that editing a prompt invalidates its recordings.
        # Without it, a rewritten prompt would replay answers given to the
        # previous one and the change would appear to have had no effect.
        "prompt_version": request.prompt_version,
        # Deliberately absent: `node` (a label, not a question), and
        # `cache_prefix` (a billing decision that cannot change the answer).
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class CassetteLibrary:
    """Reads and writes recordings on disk, one JSON file per key."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(self, key: str) -> Path:
        return self._root / f"{key}.json"

    def load(self, request: LLMRequest) -> LLMResponse:
        """Replay, or raise loudly.

        Raises:
            CassetteMissError: No recording exists for this request.
        """
        key = cassette_key(request)
        path = self.path_for(key)
        if not path.exists():
            raise CassetteMissError(key, request, path)

        stored = json.loads(path.read_text(encoding="utf-8"))
        recorded = stored["response"]
        return LLMResponse(
            text=recorded["text"],
            usage=TokenUsage(**recorded["usage"]),
            model=recorded["model"],
            stop_reason=recorded.get("stop_reason"),
            parsed=recorded.get("parsed"),
            # Always True on the way out, whatever the file says. A cassette
            # that could claim to be live would let a benchmark report
            # replayed numbers as measured ones.
            replayed=True,
            latency_ms=recorded.get("latency_ms", 0),
        )

    def save(self, request: LLMRequest, response: LLMResponse) -> Path:
        """Write a recording, with the request beside it.

        The request is stored alongside the response, not only its hash. A
        cassette directory of opaque digests is unreviewable, and the point of
        committing these files is that a human can read what the model was
        asked in a pull request.
        """
        key = cassette_key(request)
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any] = {
            "cassette_version": CASSETTE_VERSION,
            "key": key,
            "request": asdict(request) | {"messages": list(request.messages)},
            "response": {
                "text": response.text,
                "usage": asdict(response.usage),
                "model": response.model,
                "stop_reason": response.stop_reason,
                "parsed": response.parsed,
                "latency_ms": response.latency_ms,
            },
        }
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return path
