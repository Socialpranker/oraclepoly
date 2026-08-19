# Drill Report — Sign-off Template

> Phase 1 Stage 8. Format based on CISA Tabletop Exercise Packages + FFIEC IT
> Handbook BCM + FBIIC Financial Sector Cyber Exercise Template.
> См. [`research/live-drill-tabletop-2026-05/report.md`](../../research/live-drill-tabletop-2026-05/report.md).

## Report format (один scenario)

```
DRILL REPORT — <scenario_id> — <YYYY-MM-DD>
-------------------------------------------
Scenario:               <title>
Layer tested:           L1_safety | L2_risk_engine | L3_daemon_db | cross_layer | chaos
Severity:               S1 (critical) | S2 (high) | S3 (medium) | S4 (low)
Hypothesis:             <steady-state assertion, e.g. "kill_switch fires within 60s">

Pre-drill state:
  Git commit:           <sha>
  DB snapshot:          <pg_dump id или markdown checklist>
  Redis keys:           <relevant keys + values>
  EXECUTION_ENABLED:    false | true
  Mock mode:            true | false (Settings.execution_mock_mode)

Trigger (exact commands run):
  1. <bash / SQL / Python>
  2. ...

Expected outcome:
  - <log event names>
  - <TG alert pattern>
  - <DB state changes>
  - <Redis state changes>
  - <latency budget>

Observed outcome:
  <free text — exactly what happened>

Metrics:
  MTTD (Mean Time To Detect):    <seconds>
  MTTR (Mean Time To Recover):   <seconds>
  Steady-state preserved?:       YES | NO | PARTIAL

Result:                 PASS | PASS-WITH-FINDING | FAIL

Findings:
  S<X>-<finding-id>: <description>
    Severity:   S1 | S2 | S3 | S4
    Owner:      <name>
    Due:        <YYYY-MM-DD>
    Ticket:     <link if exists>

Action items:
  1. <task> — owner: <name>, due: <date>
  2. ...

Sign-off:
  Runner:     <name>, <date>, <time UTC>
  Reviewer:   <name>, <date>  (required для S1 scenarios)

Next drill (regression):
  Schedule:   <date OR "after fix-id-N merge">
```

## Cadence (post-live)

| Cadence | Scope | Trigger |
|---|---|---|
| **Pre-live (current)** | All 27 scenarios — все PASS перед EXECUTION_ENABLED=true | One-time |
| Quarterly | All 27 scenarios full regression | Calendar (Q1/Q2/Q3/Q4) |
| Monthly | 5 rotating scenarios (rolling subset) | Calendar |
| Per-release | 3-5 scenarios на изменённые layers | Code change in app/services/risk_engine.py, app/jobs/v2_execution.py, app/services/execution_safety.py, scripts/risk_daemon.py |
| Continuous chaos | Random kill_switch SET в staging | Cron weekly (если EXECUTION_ENABLED=true в staging) |

## Anti-patterns (что НЕ делать)

- **Auto-pass scenarios без observed text.** Если observed пустой → SKIPPED, не PASS.
- **Sign-off без reviewer для S1.** Solo founder bot — reviewer = ты сам через 24h cooldown (re-read).
- **Drill в production пока EXECUTION_ENABLED=false без mock.** Blast radius уже 0 — но без mock signals не inject.
- **Recovery script auto-resume без human ack** в шаге RESUMED. См. [`research/live-drill-tabletop-2026-05/report.md`](../../research/live-drill-tabletop-2026-05/report.md) §3 Anti-patterns.

## Storage

Drill reports JSON:
- [`research/live-drill-tabletop-2026-05/drill-reports/`](../../research/live-drill-tabletop-2026-05/drill-reports/) — машинно-генерируемые из `scripts/drill/run_scenarios.py`
- Human-written reports markdown: same folder, naming `<YYYY-MM-DD>-<scope>.md`
