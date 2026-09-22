"""What identifies a benchmark run.

`docs/evaluation/claims.md` states the rule absolutely: no number appears
anywhere unless a stored run produced it, and that run is identified by
``(git_sha, prompt_version, model_id, pack_version, config_hash)``. This module
is that tuple, and it is the reason invariant I7 is enforceable rather than
aspirational.

**Why each field is in it.**

- ``git_sha`` - the code. Without it "we measured 35 minutes of lead time" is
  unattributable to any version of the thing that produced it.
- ``prompt_version`` - a reworded prompt is a different system, and it leaves
  no other trace in a result file.
- ``model_id`` - the answer, not the request. A server-side fallback can serve
  a different model than the one asked for.
- ``pack_version`` - the scenarios. A number measured on pack 1.0.0 says
  nothing about pack 1.1.0.
- ``config_hash`` - every threshold that could change the answer. The detector
  threshold, the risk horizon, the verification windows. This is the field
  most likely to be left out and the one that makes a run reproducible.

``git_sha`` carries a ``-dirty`` suffix when the tree has uncommitted changes.
A number measured against code that exists only on one laptop is not
reproducible, and the suffix is what makes that visible in the stored result
rather than discovered later.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["RunProvenance", "config_hash", "current_provenance", "git_sha"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def git_sha() -> str:
    """The current commit, with ``-dirty`` when the tree has local changes.

    Returns ``"unknown"`` rather than raising when git is unavailable. A run
    that cannot identify its code is still worth storing - it is worth storing
    *and* labelled, so nobody later mistakes it for a reproducible one.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git is on PATH by design
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"

    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return sha
    return f"{sha}-dirty" if dirty else sha


def config_hash(config: dict[str, Any]) -> str:
    """A stable digest of every parameter that could change the answer.

    Sorted and tightly separated, so the digest depends on the values and not
    on the order a caller happened to build the dict in.
    """
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """The five fields `claims.md` requires, plus when the run happened."""

    git_sha: str
    prompt_version: str
    model_id: str
    pack_version: str
    config_hash: str
    started_at: datetime

    @property
    def run_id(self) -> str:
        """A short, quotable identifier.

        Built from the fields rather than random, so two runs of the same code
        over the same pack with the same configuration share an id - which is
        what makes "has this already been measured?" answerable.
        """
        digest = hashlib.blake2b(
            "|".join(
                (
                    self.git_sha,
                    self.prompt_version,
                    self.model_id,
                    self.pack_version,
                    self.config_hash,
                )
            ).encode("utf-8"),
            digest_size=6,
        ).hexdigest()
        return f"run-{digest}"

    @property
    def reproducible(self) -> bool:
        """False when the code cannot be identified, or was uncommitted.

        A report quotes this. A number from a dirty tree is a real measurement
        of something nobody else can run.
        """
        return self.git_sha != "unknown" and not self.git_sha.endswith("-dirty")

    def as_payload(self) -> dict[str, Any]:
        return {**asdict(self), "run_id": self.run_id, "reproducible": self.reproducible}


def current_provenance(
    *,
    config: dict[str, Any],
    pack_version: str = "unknown",
    prompt_version: str = "none",
    model_id: str = "none",
) -> RunProvenance:
    """Provenance for a run starting now.

    ``prompt_version`` and ``model_id`` default to ``"none"`` rather than to a
    model name, because the `rules_only` arm consults no model and recording a
    model id it never called would make the two arms indistinguishable in
    stored results - which is the one comparison the whole ablation rests on.

    ``pack_version`` defaults to ``"unknown"``, **not** to a version number.
    It was previously defaulted to ``"1.0.0"`` while the runner passed nothing,
    so every run recorded 1.0.0 whatever pack it had actually read. That was
    invisible while only one pack existed and a silent falsehood the moment a
    second one did - and it is worse than a missing value twice over, because
    the run id is a digest of this tuple, so two runs over *different* packs
    would collide on one id. A caller that forgets now says so.
    """
    return RunProvenance(
        git_sha=git_sha(),
        prompt_version=prompt_version,
        model_id=model_id,
        pack_version=pack_version,
        config_hash=config_hash(config),
        started_at=datetime.now(UTC),
    )
