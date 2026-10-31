"""The public workspace must derive from published evidence and actual policy."""

from scripts.build_workbench_data import ROOT, normalized, render


def test_public_workbench_evidence_is_reproducible_and_matches_both_copies() -> None:
    for name, content in render().items():
        for directory in [ROOT / "apps/control_tower", ROOT / "site/ui"]:
            assert normalized(directory / name) == content
