"""Synchronize only reviewed UI assets; never delete or regenerate recorded API evidence."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_ASSETS = ("index.html", "workbench.html", "engineering.html")
UI_ASSETS = (
    "coldpath.css",
    "fonts/SpaceGrotesk-Variable.ttf",
    "fonts/SpaceGrotesk-OFL.txt",
    "fonts/Manrope-Variable.ttf",
    "fonts/Manrope-OFL.txt",
    "story.mjs",
    "overview.mjs",
    "app.css",
    "app.js",
    "verification-release.json",
    "workbench.css",
    "workbench.mjs",
    "workbench-core.mjs",
    "workbench-data.json",
    "workbench-manifest.json",
    "workbench-parity.json",
)


def sync(*, check: bool = False) -> None:
    for names, destination in [(ROOT_ASSETS, ROOT / "site"), (UI_ASSETS, ROOT / "site/ui")]:
        for name in names:
            source = ROOT / "apps/control_tower" / name
            target = destination / name
            if check:
                if not target.exists() or (
                    source.read_bytes() != target.read_bytes()
                    if source.suffix == ".ttf"
                    else source.read_text(encoding="utf-8") != target.read_text(encoding="utf-8")
                ):
                    raise ValueError("Static UI is stale: " + name)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    sync(check=parser.parse_args().check)


if __name__ == "__main__":
    main()
