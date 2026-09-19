"""IncidentForge command line interface.

uv run poe forge list
uv run poe forge show compressor_degradation_pharma_01
uv run poe forge run compressor_degradation_pharma_01
uv run poe forge run-all
uv run poe forge verify
"""

from __future__ import annotations

from pathlib import Path

import typer

from simulator.incidentforge.emitters.files import compute_digest, write_run
from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.scenarios import Scenario, ScenarioPack, load_pack

app = typer.Typer(
    add_completion=False,
    help="IncidentForge: seeded cold-chain scenario simulator.",
    no_args_is_help=True,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PACK = _REPO_ROOT / "data" / "scenarios" / "pack_v1"
_DEFAULT_OUT = _REPO_ROOT / "data" / "generated"


def _load(pack_dir: Path) -> ScenarioPack:
    try:
        return load_pack(pack_dir)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(f"Failed to load scenario pack: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _get(pack: ScenarioPack, scenario_id: str) -> Scenario:
    try:
        return pack.scenarios[scenario_id]
    except KeyError:
        typer.secho(f"Unknown scenario {scenario_id!r}.", fg=typer.colors.RED, err=True)
        typer.echo("Available: " + ", ".join(sorted(pack.scenarios)), err=True)
        raise typer.Exit(1) from None


@app.command("list")
def list_scenarios(
    pack_dir: Path = typer.Option(_DEFAULT_PACK, "--pack", help="Scenario pack directory"),
) -> None:
    """List the scenarios in a pack."""
    pack = _load(pack_dir)
    typer.secho(f"Pack {pack.pack_version}  ({len(pack.scenarios)} scenarios)", bold=True)
    typer.echo()
    for sid in sorted(pack.scenarios):
        scenario = pack.scenarios[sid]
        gt = scenario.ground_truth
        breach = f"breach@{gt.breach_at_min}min" if gt.breach_occurs else "no breach"
        typer.echo(f"  {sid}")
        typer.echo(f"      {gt.root_cause}  |  {breach}  |  seed {scenario.seed}")


@app.command("show")
def show_scenario(
    scenario_id: str,
    pack_dir: Path = typer.Option(_DEFAULT_PACK, "--pack"),
    every: int = typer.Option(15, "--every", help="Print one row every N minutes"),
) -> None:
    """Run a scenario and print its trajectory without writing files."""
    scenario = _get(_load(pack_dir), scenario_id)
    result = run_scenario(scenario)

    lo, hi = scenario.cargo.permitted_min_c, scenario.cargo.permitted_max_c

    typer.secho(f"\n{scenario.scenario_id}", bold=True)
    typer.echo(f"  {scenario.description.strip()}")
    typer.echo(f"\n  Envelope {lo}-{hi} C   cargo ${scenario.cargo.value_usd:,.0f}")
    typer.echo(f"  Seed {scenario.seed}   duration {scenario.duration_minutes} min")

    typer.echo("\n  min |  reported |     true | ambient |  rpm | duty | codes")
    typer.echo("  ----+-----------+----------+---------+------+------+------")
    for frame, event in zip(result.ground_truth, result.events, strict=True):
        if frame.sequence % every and frame.sequence != scenario.duration_minutes - 1:
            continue
        flag = "!" if frame.in_breach else (">" if frame.saturated else " ")
        typer.echo(
            f"  {frame.sequence:3d}{flag}| {event.cargo_temp_c:9.2f} "
            f"| {frame.true_cargo_temp_c:8.2f} | {event.ambient_temp_c:7.1f} "
            f"| {event.compressor_rpm:4.0f} | {frame.duty_cycle:4.2f} "
            f"| {','.join(event.fault_codes) or '-'}"
        )

    typer.echo("\n  Outcome")
    typer.echo(f"    saturation       : {result.first_saturation_minute}")
    typer.echo(f"    actual breach    : {result.actual_breach_minute}")
    typer.echo(f"    reported breach  : {result.reported_breach_minute}")

    if result.sensor_lied:
        typer.secho(
            "\n    The instrument disagreed with reality. Telemetry alone "
            "cannot resolve this scenario.",
            fg=typer.colors.YELLOW,
        )

    if result.first_saturation_minute is not None and result.actual_breach_minute:
        warning = result.actual_breach_minute - result.first_saturation_minute
        typer.secho(
            f"\n    {warning} minutes between the unit saturating and the envelope being breached.",
            fg=typer.colors.CYAN,
        )
    typer.echo()


@app.command("run")
def run_one(
    scenario_id: str,
    pack_dir: Path = typer.Option(_DEFAULT_PACK, "--pack"),
    out_dir: Path = typer.Option(_DEFAULT_OUT, "--out"),
) -> None:
    """Run one scenario and write its artifacts."""
    pack = _load(pack_dir)
    scenario = _get(pack, scenario_id)
    result = run_scenario(scenario)
    artifacts = write_run(result, out_dir, pack_version=pack.pack_version)

    typer.secho(f"{scenario_id}", bold=True)
    typer.echo(f"  events  {len(result.events)}")
    typer.echo(f"  digest  {artifacts.digest[:16]}...")
    typer.echo(f"  wrote   {artifacts.telemetry_path.parent}")


@app.command("run-all")
def run_all(
    pack_dir: Path = typer.Option(_DEFAULT_PACK, "--pack"),
    out_dir: Path = typer.Option(_DEFAULT_OUT, "--out"),
) -> None:
    """Run every scenario in the pack."""
    pack = _load(pack_dir)
    for sid in sorted(pack.scenarios):
        result = run_scenario(pack.scenarios[sid])
        artifacts = write_run(result, out_dir, pack_version=pack.pack_version)
        typer.echo(f"  {sid:<40} {artifacts.digest[:16]}...")
    typer.secho(f"\nWrote {len(pack.scenarios)} runs to {out_dir}", fg=typer.colors.GREEN)


@app.command("verify")
def verify(
    pack_dir: Path = typer.Option(_DEFAULT_PACK, "--pack"),
) -> None:
    """Check determinism and that ground truth matches the physics.

    Two properties, both of which silently invalidate every benchmark number
    if they break: a scenario must produce the same stream twice, and it must
    breach at the minute its ground truth declares.
    """
    pack = _load(pack_dir)
    failures = 0

    for sid in sorted(pack.scenarios):
        scenario = pack.scenarios[sid]
        first = run_scenario(scenario)
        second = run_scenario(scenario)

        deterministic = compute_digest(first) == compute_digest(second)
        matches_truth = first.actual_breach_minute == scenario.ground_truth.breach_at_min

        if deterministic and matches_truth:
            typer.secho(f"  PASS  {sid}", fg=typer.colors.GREEN)
            continue

        failures += 1
        typer.secho(f"  FAIL  {sid}", fg=typer.colors.RED)
        if not deterministic:
            typer.echo("          two runs produced different output")
        if not matches_truth:
            typer.echo(
                f"          declares breach at {scenario.ground_truth.breach_at_min}, "
                f"simulation breaches at {first.actual_breach_minute}"
            )

    if failures:
        typer.secho(f"\n{failures} scenario(s) failed verification.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho(f"\nAll {len(pack.scenarios)} scenarios verified.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
