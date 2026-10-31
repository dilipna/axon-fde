"""Attach exact source/tree identity to the static workspace without changing its evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    allowed = {
        ("ls-files", "-z"),
        ("rev-parse", "HEAD"),
        ("rev-parse", "HEAD^{tree}"),
        ("status", "--porcelain", "--untracked-files=normal"),
    }
    if args not in allowed:
        raise ValueError("Unsupported identity command")
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for source identity")
    # No shell, fixed repository, and a closed subcommand allowlist.
    return subprocess.check_output([executable, *args], cwd=ROOT).decode("utf-8").strip()  # noqa: S603


def build_identity() -> dict[str, object]:
    files = {}
    for name in git("ls-files", "-z").split("\0"):
        if not name:
            continue
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unexpected missing source or symlink: " + name)
        raw = path.read_bytes()
        if b"\0" not in raw:
            with suppress(UnicodeDecodeError):
                raw = raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
        files[name] = hashlib.sha256(raw).hexdigest()
    return {
        "schema": 1,
        "source_commit": git("rev-parse", "HEAD"),
        "source_tree": git("rev-parse", "HEAD^{tree}"),
        "dirty_source": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "source_files_sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "source_files": files,
        "scope": "UTF-8/LF source consistency; not fleet freshness or model quality.",
    }


def main() -> None:
    result = build_identity()
    content = json.dumps(result, sort_keys=True, indent=2) + "\n"
    for parent in [ROOT / "apps/control_tower", ROOT / "site/ui"]:
        (parent / "workbench-release.json").write_text(content, encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "source_files"}))


if __name__ == "__main__":
    main()
