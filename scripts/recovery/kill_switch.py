"""Kill switch recovery script — Phase 1 Stage 8.

Do-nothing scripting pattern (Slimmon 2019). 7-state machine:
  TRIGGERED → ACKNOWLEDGED → DIAGNOSING → FIXING → VERIFYING → SOAKING → RESUMED

Каждый step: interactive (требует human confirmation) либо automated
(verification). Single rule: **automate verification, never automate
the decision to resume**.

Idempotent: state JSON persists в /tmp/oracle_recovery_<id>.json.
Re-run после crash продолжает с last completed step.

Usage:
  python scripts/recovery/kill_switch.py recover  # start new recovery
  python scripts/recovery/kill_switch.py recover --resume <id>  # resume crashed
  python scripts/recovery/kill_switch.py list  # show in-progress recoveries

См. research/live-drill-tabletop-2026-05/report.md §3 Recovery.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

STATE_DIR = Path("/tmp/oracle_recovery")
KILL_SWITCH_KEY = "bot:kill_switch"
RECON_STRIKES_KEY = "bot:reconciliation_strikes"

STATES = (
    "TRIGGERED",
    "ACKNOWLEDGED",
    "DIAGNOSING",
    "FIXING",
    "VERIFYING",
    "SOAKING",
    "RESUMED",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _state_path(recovery_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{recovery_id}.json"


def _load_state(recovery_id: str) -> dict[str, Any]:
    p = _state_path(recovery_id)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _save_state(recovery_id: str, state: dict[str, Any]) -> None:
    """Atomic write: tmp file + rename (prevent corruption на crash)."""
    p = _state_path(recovery_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def _append_audit(state: dict[str, Any], event: str, **kw: Any) -> None:
    state.setdefault("audit_log", []).append(
        {"ts": _now_iso(), "event": event, **kw}
    )


def _transition(state: dict[str, Any], to_state: str) -> None:
    """Move to next state в state machine, validate allowed transition."""
    current = state.get("state", "TRIGGERED")
    current_idx = STATES.index(current)
    next_idx = STATES.index(to_state)
    # Allow: forward by 1 OR back to DIAGNOSING from FIXING (retry).
    if next_idx == current_idx + 1 or (current == "FIXING" and to_state == "DIAGNOSING"):
        state["state"] = to_state
        _append_audit(state, "state_transition", from_state=current, to_state=to_state)
    else:
        raise ValueError(f"Invalid transition: {current} → {to_state}")


# --- State handlers --------------------------------------------------------


def step_acknowledge(state: dict[str, Any]) -> None:
    """TRIGGERED → ACKNOWLEDGED. Human ack что kill_switch видим."""
    click.echo(click.style("STEP 1: ACKNOWLEDGE", fg="cyan", bold=True))
    click.echo("kill_switch SET requires human investigation.")
    click.echo("First — acknowledge that you're handling это (NOT auto-recovery).")
    if not click.confirm("Acknowledge kill_switch + take ownership?"):
        click.echo("Aborted. Re-run when ready.")
        sys.exit(1)
    state["acknowledged_at"] = _now_iso()
    _transition(state, "ACKNOWLEDGED")


def step_diagnose(state: dict[str, Any]) -> None:
    """ACKNOWLEDGED → DIAGNOSING. Read-only snapshot."""
    click.echo(click.style("STEP 2: DIAGNOSE (read-only)", fg="cyan", bold=True))
    click.echo("Read state. Do NOT change anything. Write findings in audit log.")
    click.echo()
    click.echo("Suggested commands:")
    click.echo("  ssh <your-server> 'cd /root/oracle && docker exec oraclepoly-redis \\")
    click.echo("    redis-cli -a \"$(grep REDIS_PASSWORD .env | cut -d= -f2)\" GET bot:kill_switch'")
    click.echo("  ssh <your-server> 'docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly \\")
    click.echo("    -c \"SELECT status, COUNT(*) FROM execution_orders WHERE settled_at IS NULL GROUP BY status\"'")
    click.echo("  ssh <your-server> 'docker logs oraclepoly-risk-daemon --tail 50'")
    click.echo("  ssh <your-server> 'docker logs oraclepoly-backend --tail 50 | grep -iE \"error|kill\"'")
    click.echo()
    root_cause = click.prompt(
        click.style("Root cause identified (free text)", fg="green"),
        type=str,
    )
    state["root_cause"] = root_cause
    _append_audit(state, "diagnosis_completed", root_cause=root_cause)
    _transition(state, "DIAGNOSING")


def step_fix(state: dict[str, Any]) -> None:
    """DIAGNOSING → FIXING → VERIFYING (либо обратно DIAGNOSING)."""
    click.echo(click.style("STEP 3: FIX root cause", fg="cyan", bold=True))
    click.echo(f"Root cause: {state.get('root_cause', '(not set)')}")
    click.echo()
    click.echo("Apply fix (config change / migration / rollback / etc).")
    click.echo(click.style("⚠ DO NOT auto-unlock kill_switch yet. Fix first.", fg="yellow"))
    click.echo()
    fix_description = click.prompt(
        click.style("Fix applied (free text)", fg="green"),
        type=str,
    )
    state.setdefault("fix_attempts", []).append({"ts": _now_iso(), "fix": fix_description})
    _append_audit(state, "fix_applied", fix=fix_description)

    if click.confirm("Fix successful?"):
        _transition(state, "FIXING")
    else:
        # Go back to DIAGNOSING (retry).
        _append_audit(state, "fix_failed_returning_to_diagnose")
        state["state"] = "ACKNOWLEDGED"  # back to before diagnose (re-diagnose)


def step_verify(state: dict[str, Any]) -> None:
    """FIXING → VERIFYING. Smoke test (NO real orders)."""
    click.echo(click.style("STEP 4: VERIFY", fg="cyan", bold=True))
    click.echo("Smoke test: dry-run signal, NO real order. Check:")
    click.echo("  - Reconcile clean (no orphans)")
    click.echo("  - Daemon heartbeat fresh")
    click.echo("  - Main heartbeat fresh")
    click.echo("  - DB constraints не violated на recent INSERTs")
    click.echo()
    if not click.confirm("Verification passed?"):
        click.echo("Cannot proceed. Re-diagnose or fix further.")
        state["state"] = "ACKNOWLEDGED"
        _append_audit(state, "verification_failed_returning_to_acknowledge")
        return
    _transition(state, "VERIFYING")


def step_soak(state: dict[str, Any]) -> None:
    """VERIFYING → SOAKING. Unlock kill_switch + monitor 15 min."""
    click.echo(click.style("STEP 5: SOAK", fg="cyan", bold=True))
    click.echo("This step:")
    click.echo("  1. DEL bot:kill_switch + bot:reconciliation_strikes")
    click.echo("  2. EXECUTION_ENABLED остаётся false (read-only)")
    click.echo("  3. Watch logs 15 min")
    click.echo("  4. Abort if any drift signal")
    click.echo()
    if not click.confirm("Ready для soak?"):
        return

    click.echo("Run on prod:")
    click.echo("  ssh <your-server> 'docker exec oraclepoly-redis redis-cli \\")
    click.echo("    -a \"$(grep REDIS_PASSWORD /root/oracle/.env | cut -d= -f2)\" \\")
    click.echo("    DEL bot:kill_switch bot:reconciliation_strikes'")
    click.echo()
    if not click.confirm("kill_switch DEL'd?"):
        return

    state["soak_started_at"] = _now_iso()
    _append_audit(state, "soak_started")
    click.echo()
    click.echo("Now watch logs for 15 min. Abort with Ctrl+C if anything bad.")
    click.echo("  ssh <your-server> 'docker logs -f oraclepoly-risk-daemon | grep -iE \"kill|drift\"'")
    click.echo("  ssh <your-server> 'docker logs -f oraclepoly-backend | grep -iE \"error|kill\"'")
    click.echo()
    click.echo("Sleeping 15 min...")
    for i in range(15):
        time.sleep(60)
        click.echo(f"  Soak: {i + 1}/15 min")
    state["soak_completed_at"] = _now_iso()
    _append_audit(state, "soak_completed_clean")
    _transition(state, "SOAKING")


def step_resume(state: dict[str, Any]) -> None:
    """SOAKING → RESUMED. Human ack для EXECUTION_ENABLED=true."""
    click.echo(click.style("STEP 6: RESUME", fg="cyan", bold=True))
    click.echo("Soak clean. Final decision:")
    click.echo("  - Was the soak truly clean (no log warnings)?")
    click.echo("  - Is business context OK for resume (no макро event in flight)?")
    click.echo("  - Risk metrics within budget?")
    click.echo()
    click.echo(click.style("⚠ HUMAN JUDGMENT REQUIRED. Никакого auto-resume.", fg="yellow"))
    if not click.confirm("Resume trading (EXECUTION_ENABLED=true)?"):
        click.echo("Holding off. State saved.")
        return
    state["resumed_at"] = _now_iso()
    _append_audit(state, "resumed")
    _transition(state, "RESUMED")
    click.echo(click.style("Recovery complete. Open postmortem TODO.", fg="green"))


# --- CLI -------------------------------------------------------------------


STEP_HANDLERS = {
    "TRIGGERED": step_acknowledge,
    "ACKNOWLEDGED": step_diagnose,
    "DIAGNOSING": step_fix,
    "FIXING": step_verify,
    "VERIFYING": step_soak,
    "SOAKING": step_resume,
}


@click.group()
def cli() -> None:
    """Kill switch recovery (do-nothing scripting)."""


@cli.command(name="recover")
@click.option("--resume", "resume_id", default=None, help="Resume in-progress recovery")
def cmd_recover(resume_id: str | None) -> None:
    """Run kill_switch recovery interactively."""
    if resume_id:
        recovery_id = resume_id
        state = _load_state(recovery_id)
        if not state:
            click.echo(click.style(f"No state found for: {recovery_id}", fg="red"))
            sys.exit(1)
        click.echo(f"Resuming recovery {recovery_id} from state: {state.get('state')}")
    else:
        recovery_id = uuid.uuid4().hex[:8]
        state = {
            "recovery_id": recovery_id,
            "state": "TRIGGERED",
            "triggered_at": _now_iso(),
            "audit_log": [],
        }
        _save_state(recovery_id, state)
        click.echo(f"Started recovery {recovery_id}")

    # State machine loop.
    while state["state"] != "RESUMED":
        current = state["state"]
        handler = STEP_HANDLERS.get(current)
        if handler is None:
            click.echo(click.style(f"No handler for state: {current}", fg="red"))
            break
        handler(state)
        _save_state(recovery_id, state)
        if state["state"] == current:
            # Step не продвинулся (например fix failed) — выход для re-run later.
            click.echo("State unchanged. Re-run recovery когда ready.")
            sys.exit(0)

    click.echo(click.style(f"\n=== Recovery {recovery_id} RESUMED ===", fg="green", bold=True))


@cli.command(name="list")
def cmd_list() -> None:
    """List in-progress and completed recoveries."""
    if not STATE_DIR.exists():
        click.echo("No recoveries.")
        return
    files = sorted(STATE_DIR.glob("*.json"))
    if not files:
        click.echo("No recoveries.")
        return
    for f in files:
        try:
            s = json.loads(f.read_text())
            click.echo(f"  {s.get('recovery_id', f.stem)}: state={s.get('state')} triggered={s.get('triggered_at')}")
        except Exception as e:
            click.echo(f"  {f.name}: ERROR {e}")


if __name__ == "__main__":
    cli()
