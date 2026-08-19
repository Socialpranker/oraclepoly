"""Drill scenarios runner — Phase 1 Stage 8.

Interactive Click CLI для запуска drill scenarios. Pattern: do-nothing
scripting (Slimmon 2019). Каждый scenario:
  1. Print pre-state setup → wait for human confirmation.
  2. Print trigger action → wait for confirmation.
  3. Print expected → wait for observation input.
  4. Print recovery → wait for confirmation.
  5. Write drill report JSON.

В отличие от full automation: human decides "pass/fail/skip" — это
proves что чек проверил реально, не slip'нул в auto-pass.

Usage:
  python scripts/drill/run_scenarios.py list
  python scripts/drill/run_scenarios.py run F01
  python scripts/drill/run_scenarios.py run --all
  python scripts/drill/run_scenarios.py run --layer L3_daemon_db
  python scripts/drill/run_scenarios.py run --severity S1

См. research/live-drill-tabletop-2026-05/report.md §3 Sign-off template.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

# Ensure imports работают и в `python scripts/...` mode
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.drill.scenarios import (  # noqa: E402
    ALL_SCENARIOS,
    Scenario,
    get_scenario,
    scenarios_by_layer,
    scenarios_by_severity,
)

REPORTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "live-drill-tabletop-2026-05"
    / "drill-reports"
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def _print_scenario_header(s: Scenario) -> None:
    click.echo()
    click.echo("=" * 72)
    click.echo(click.style(f"  {s.id} — {s.title}", fg="cyan", bold=True))
    click.echo(f"  Layer:     {s.layer}")
    click.echo(f"  Severity:  {s.severity}")
    click.echo(f"  Hypothesis: {s.hypothesis}")
    click.echo("=" * 72)


def _print_steps(label: str, steps: list[str]) -> None:
    click.echo()
    click.echo(click.style(f"--- {label} ---", fg="yellow"))
    for i, step in enumerate(steps, 1):
        click.echo(f"  {i}. {step}")


def _confirm(question: str) -> bool:
    return click.confirm(click.style(question, fg="green"), default=False)


def _run_one_scenario(s: Scenario, *, output_dir: Path) -> dict:
    """Interactive run — return report dict."""
    started_at = _now_iso()
    _print_scenario_header(s)

    _print_steps("PRE-STATE setup", s.pre_state)
    if not _confirm("Pre-state setup завершён?"):
        return {
            "id": s.id, "started_at": started_at, "completed_at": _now_iso(),
            "result": "SKIPPED", "note": "Pre-state not completed",
        }

    _print_steps("TRIGGER", s.trigger)
    if not _confirm("Trigger executed?"):
        return {
            "id": s.id, "started_at": started_at, "completed_at": _now_iso(),
            "result": "SKIPPED", "note": "Trigger not executed",
        }

    _print_steps("EXPECTED outcome", s.expected)
    observed = click.prompt(
        click.style("OBSERVED outcome (free text)", fg="green"),
        type=str,
    )

    result_choice = click.prompt(
        click.style("Result", fg="green"),
        type=click.Choice(["PASS", "PASS-WITH-FINDING", "FAIL"], case_sensitive=False),
    )

    findings = ""
    if result_choice.upper() != "PASS":
        findings = click.prompt(
            click.style("Findings (numbered, with severity)", fg="green"),
            type=str,
        )

    _print_steps("RECOVERY", s.recovery)
    if not _confirm("Recovery completed?"):
        click.echo(click.style("⚠ Recovery NOT confirmed — state может быть dirty!", fg="red"))

    completed_at = _now_iso()
    report = {
        "id": s.id,
        "title": s.title,
        "layer": s.layer,
        "severity": s.severity,
        "hypothesis": s.hypothesis,
        "started_at": started_at,
        "completed_at": completed_at,
        "result": result_choice.upper(),
        "observed": observed,
        "findings": findings,
        "pre_state": s.pre_state,
        "trigger": s.trigger,
        "expected": s.expected,
        "recovery": s.recovery,
    }

    # Persist report.
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{s.id}_{started_at}.json"
    out_path.write_text(json.dumps(report, indent=2))
    click.echo(click.style(f"  Report saved: {out_path}", fg="cyan"))

    return report


@click.group()
def cli() -> None:
    """Drill scenarios runner."""


@cli.command(name="list")
@click.option("--layer", default=None, help="Filter by layer")
@click.option("--severity", default=None, help="Filter by severity")
def cmd_list(layer: str | None, severity: str | None) -> None:
    """List available scenarios."""
    scenarios = ALL_SCENARIOS
    if layer:
        scenarios = [s for s in scenarios if s.layer == layer]
    if severity:
        scenarios = [s for s in scenarios if s.severity == severity]
    click.echo(f"Total: {len(scenarios)}")
    for s in scenarios:
        click.echo(f"  {s.id}  [{s.severity}/{s.layer}]  {s.title}")


@cli.command(name="run")
@click.argument("scenario_id", required=False, default=None)
@click.option("--all", "run_all", is_flag=True, help="Run all scenarios")
@click.option("--layer", default=None)
@click.option("--severity", default=None)
def cmd_run(
    scenario_id: str | None,
    run_all: bool,
    layer: str | None,
    severity: str | None,
) -> None:
    """Run drill scenario(s) interactively."""
    if scenario_id:
        s = get_scenario(scenario_id)
        if s is None:
            click.echo(click.style(f"Unknown scenario: {scenario_id}", fg="red"))
            sys.exit(1)
        _run_one_scenario(s, output_dir=REPORTS_DIR)
        return

    if run_all:
        scenarios = ALL_SCENARIOS
    elif layer:
        scenarios = scenarios_by_layer(layer)  # type: ignore[arg-type]
    elif severity:
        scenarios = scenarios_by_severity(severity)  # type: ignore[arg-type]
    else:
        click.echo("Pass --all, --layer, --severity, или scenario id (F01, X02, C03).")
        sys.exit(1)

    click.echo(f"Running {len(scenarios)} scenarios...")
    summary: list[dict] = []
    for s in scenarios:
        result = _run_one_scenario(s, output_dir=REPORTS_DIR)
        summary.append(result)

    # Final summary.
    click.echo()
    click.echo(click.style("=== DRILL SUMMARY ===", fg="cyan", bold=True))
    counts = {"PASS": 0, "PASS-WITH-FINDING": 0, "FAIL": 0, "SKIPPED": 0}
    for r in summary:
        counts[r.get("result", "SKIPPED")] = counts.get(r.get("result"), 0) + 1
    for k, v in counts.items():
        click.echo(f"  {k}: {v}")


if __name__ == "__main__":
    cli()
