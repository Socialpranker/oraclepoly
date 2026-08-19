"""Drill scenarios catalogue — Phase 1 Stage 8.

27 scenarios total = 12 functional + 8 cascades + 7 chaos. См.
research/live-drill-tabletop-2026-05/report.md §Findings.2.

Каждый Scenario — pure data (нет execution logic). Execution в
`scripts/drill/run_scenarios.py`. Это позволяет unit-test catalogue
независимо от runner.

Severity:
  - S1 critical: money path, blocks production.
  - S2 high: integration bug, needs fix перед live.
  - S3 medium: edge case, low-frequency.
  - S4 low: cosmetic / log quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["S1", "S2", "S3", "S4"]
Layer = Literal["L1_safety", "L2_risk_engine", "L3_daemon_db", "cross_layer", "chaos"]


@dataclass(frozen=True)
class Scenario:
    """Drill scenario specification (pure data).

    Attributes:
        id: scenario identifier (e.g. "F01", "C03", "X05").
        title: short human-readable name.
        layer: which layer being tested.
        severity: blast radius if scenario fails.
        hypothesis: steady-state assertion ДО drill ("kill_switch fires within 60s").
        pre_state: setup steps (Redis keys, DB rows, ENV).
        trigger: action to execute.
        expected: assertion (log events, TG alert pattern, DB state).
        recovery: cleanup steps after drill.
    """

    id: str
    title: str
    layer: Layer
    severity: Severity
    hypothesis: str
    pre_state: list[str] = field(default_factory=list)
    trigger: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    recovery: list[str] = field(default_factory=list)


# --- Functional scenarios (12, from stub) ---------------------------------

FUNCTIONAL_SCENARIOS: list[Scenario] = [
    Scenario(
        id="F01",
        title="Kill switch SET manually rejects all signals",
        layer="L1_safety",
        severity="S1",
        hypothesis="SET bot:kill_switch → execution_safety.evaluate() returns allowed=False для всех signals",
        pre_state=["EXECUTION_ENABLED=true (mock mode)"],
        trigger=["redis-cli SET bot:kill_switch 'drill_test'"],
        expected=[
            "Все 10 inject'нутых signals → status='rejected'",
            "rejection_reason содержит 'kill_switch'",
            "0 orders placed в FakeClobClient",
        ],
        recovery=["redis-cli DEL bot:kill_switch"],
    ),
    Scenario(
        id="F02",
        title="Per-bet cap 6% reduces to 5%",
        layer="L2_risk_engine",
        severity="S2",
        hypothesis="risk_max_per_bet_pct=0.05, bankroll=$1000, proposed $60 → approved $50",
        pre_state=["risk_max_per_bet_pct=0.05", "bankroll=$1000"],
        trigger=["Inject signal stake=$60"],
        expected=[
            "approved_stake=$50",
            "reductions includes 'per_bet_cap'",
            "Order placed с stake=$50",
        ],
        recovery=["fake_client.reset_state()"],
    ),
    Scenario(
        id="F03",
        title="Category cap blocks при 30% used",
        layer="L2_risk_engine",
        severity="S2",
        hypothesis="Sports cat 30% × $10k=$3000 used → новый Sports signal blocked",
        pre_state=["Pre-fill 30 orders Sports category $100 each"],
        trigger=["Inject Sports signal stake=$100"],
        expected=[
            "approved_stake=$0, is_blocked=True",
            "reductions includes 'category_cap'",
            "Rejection reason содержит 'risk_block:category_cap'",
        ],
        recovery=["fake_client.reset_state()"],
    ),
    Scenario(
        id="F04",
        title="Reconciliation drift orphans 3× → kill",
        layer="L3_daemon_db",
        severity="S1",
        hypothesis="3 minute-runs с orphans>0 → SET bot:kill_switch",
        pre_state=["Mock 2 orphan trades в FakeClobClient.get_trades()"],
        trigger=["Run execution-reconcile 3 times (manual)"],
        expected=[
            "bot:reconciliation_strikes incremented 1→2→3",
            "After 3rd: bot:kill_switch SET с reason='reconciliation_drift:orphans=2'",
            "TG kill alert sent",
        ],
        recovery=[
            "redis-cli DEL bot:kill_switch bot:reconciliation_strikes",
            "fake_client.reset_state()",
        ],
    ),
    Scenario(
        id="F05",
        title="Exposure 65% > hard cap 60% → daemon kill",
        layer="L3_daemon_db",
        severity="S1",
        hypothesis="risk_daemon.check_exposure: open=$650/$1000 → kill",
        pre_state=["Pre-fill 13 orders $50 each (live mode)"],
        trigger=["Run risk_daemon --check-once"],
        expected=[
            "check returns ok=False",
            "bot:kill_switch SET с reason='exposure_hard_cap:65.0%>60%'",
        ],
        recovery=["redis-cli DEL bot:kill_switch", "fake_client.reset_state()"],
    ),
    Scenario(
        id="F06",
        title="Main backend down 6min → daemon kill",
        layer="L3_daemon_db",
        severity="S1",
        hypothesis="bot:risk_heartbeat stale > 300s → daemon SET kill_switch",
        pre_state=["Backend running, daemon running"],
        trigger=[
            "docker stop oraclepoly-backend",
            "Wait 360s",
            "Run risk_daemon --check-once",
        ],
        expected=[
            "check_main_alive returns ok=False, reason='main_heartbeat_stale'",
            "bot:kill_switch SET",
        ],
        recovery=["docker start oraclepoly-backend", "redis-cli DEL bot:kill_switch"],
    ),
    Scenario(
        id="F07",
        title="Daemon down 6min → main self_check kill",
        layer="cross_layer",
        severity="S1",
        hypothesis="bot:risk_daemon_alive stale > 300s → main SET kill_switch",
        pre_state=["Backend running, daemon running"],
        trigger=[
            "docker stop oraclepoly-risk-daemon",
            "Wait 360s (main self_check loop runs)",
        ],
        expected=[
            "set_kill_if_daemon_dead returns True",
            "bot:kill_switch SET с reason='main_self_check:risk_daemon_dead'",
        ],
        recovery=[
            "docker start oraclepoly-risk-daemon",
            "redis-cli DEL bot:kill_switch",
        ],
    ),
    Scenario(
        id="F08",
        title="Drawdown -25% → stake ×0.5",
        layer="L2_risk_engine",
        severity="S2",
        hypothesis="peak=$10k, bank=$7.5k → drawdown 25% > 20% → stake halved",
        pre_state=["Settings.risk_drawdown_threshold=0.20", "peak_bank=10000"],
        trigger=["Inject signal stake=$100 при bank=$7500"],
        expected=[
            "approved_stake=$50 (halved)",
            "reductions includes 'drawdown_reduce'",
        ],
        recovery=["redis-cli SET bot:risk:peak_bank_usd 1000"],
    ),
    Scenario(
        id="F09",
        title="Daily -3% soft → block new orders",
        layer="L1_safety",
        severity="S2",
        hypothesis="realized+mtm < -3% × bank → check_daily_pnl_limit_mtm blocks",
        pre_state=["Inject settled losses -$30 за день, bankroll=$1000"],
        trigger=["Inject new signal"],
        expected=[
            "verdict.allowed=False",
            "reason='daily_loss_limit_mtm:...'",
        ],
        recovery=["fake_client.reset_state()"],
    ),
    Scenario(
        id="F10",
        title="Daily -5% hard → daemon kill",
        layer="L3_daemon_db",
        severity="S1",
        hypothesis="realized+mtm < -5% × bank → daemon SET kill_switch",
        pre_state=["Inject realized -$40 + mtm -$15 = -$55 на $1000"],
        trigger=["Run risk_daemon --check-once"],
        expected=[
            "check_daily_pnl_mtm returns ok=False",
            "bot:kill_switch SET с reason='daemon:daily_pnl_hard:...'",
        ],
        recovery=["redis-cli DEL bot:kill_switch", "fake_client.reset_state()"],
    ),
    Scenario(
        id="F11",
        title="15 orders/min anomaly → daemon kill",
        layer="L3_daemon_db",
        severity="S2",
        hypothesis="recent count > 10 threshold → kill",
        pre_state=["Inject 15 orders за 60 sec window"],
        trigger=["Run risk_daemon --check-once"],
        expected=[
            "check_anomalies returns ok=False",
            "bot:kill_switch SET с reason='anomaly_high_order_rate:15/min'",
        ],
        recovery=["redis-cli DEL bot:kill_switch", "fake_client.reset_state()"],
    ),
    Scenario(
        id="F12",
        title="stake > 500 DB CHECK fail",
        layer="L3_daemon_db",
        severity="S2",
        hypothesis="execution_orders_stake_max CHECK fires на INSERT stake=999",
        pre_state=["Migration 20260520131136 applied"],
        trigger=["INSERT execution_orders ... stake=999.0"],
        expected=[
            "asyncpg.CheckViolationError raised",
            "Log event 'execution_order_constraint_violated'",
            "TG admin alert sent",
        ],
        recovery=["Nothing (INSERT rolled back)"],
    ),
]

# --- Cascade scenarios (8, новые после deep research) ---------------------

CASCADE_SCENARIOS: list[Scenario] = [
    Scenario(
        id="X01",
        title="Latent legacy code activation (Knight Capital pattern)",
        layer="cross_layer",
        severity="S1",
        hypothesis="Старый detector через config refresh → stake>500 CHECK + audit diff",
        pre_state=["Detector D_old в config disabled"],
        trigger=["Enable D_old, run pipeline, attempt stake=$600"],
        expected=[
            "DB CHECK rejects stake=$600",
            "audit log shows config change",
            "No orders placed",
        ],
        recovery=["Disable D_old", "fake_client.reset_state()"],
    ),
    Scenario(
        id="X02",
        title="Race: kill_switch SET одновременно с in-flight order",
        layer="cross_layer",
        severity="S1",
        hypothesis="kill_switch SET между risk_engine pass и execution dispatch → idempotent",
        pre_state=["Order в risk_engine evaluated, готов к dispatch"],
        trigger=["SET kill_switch + run execution dispatch parallel"],
        expected=[
            "Либо order rejected (kill seen) либо placed (lock acquired before kill)",
            "No duplicate orders (UNIQUE constraint enforces)",
            "Orphan detection catches placed order via reconcile",
        ],
        recovery=["redis-cli DEL bot:kill_switch"],
    ),
    Scenario(
        id="X03",
        title="Reconciliation lag + 10 orders/min burst",
        layer="cross_layer",
        severity="S2",
        hypothesis="orphans>0 + recent>10 → either layer triggers, no double-kill race",
        pre_state=["Inject 3 orphans + 15 orders/min"],
        trigger=["Run reconcile + daemon concurrent"],
        expected=[
            "bot:kill_switch SET ONE time (preserve first cause)",
            "Reason — либо 'reconciliation_drift' либо 'anomaly_high_order_rate' (first wins)",
            "No race-condition double SET",
        ],
        recovery=["redis-cli DEL bot:kill_switch bot:reconciliation_strikes"],
    ),
    Scenario(
        id="X04",
        title="Daemon false positive — recovery flow",
        layer="cross_layer",
        severity="S2",
        hypothesis="Manual SET kill_switch ошибочно → recovery script unlock корректно",
        pre_state=["redis-cli SET bot:kill_switch 'false_positive_test'"],
        trigger=["Run scripts/recovery/kill_switch.py через 6 steps"],
        expected=[
            "Recovery state machine: TRIGGERED→ACKNOWLEDGED→DIAGNOSING→FIXING→VERIFYING→SOAKING→RESUMED",
            "audit log JSON written",
            "bot:kill_switch DEL после успешного soak",
        ],
        recovery=["redis-cli DEL bot:kill_switch (если ещё set)"],
    ),
    Scenario(
        id="X05",
        title="Polymarket WSS flap 3× reconnects за 60s",
        layer="cross_layer",
        severity="S2",
        hypothesis="WSS disconnects не дублируют orders, market state не stale",
        pre_state=["WSS subscription active"],
        trigger=["Toxiproxy reset_peer 3× в 60s window"],
        expected=[
            "WSS reconnects after each disconnect",
            "Open orders не double-submitted",
            "markets.current_price обновляется после reconnect",
        ],
        recovery=["Reset Toxiproxy"],
    ),
    Scenario(
        id="X06",
        title="DB connection pool exhaustion",
        layer="cross_layer",
        severity="S2",
        hypothesis="asyncpg pool 0 free → risk_engine fails CLOSED (block new), не OPEN",
        pre_state=["asyncpg pool max_size=3"],
        trigger=["Acquire 3 connections + hold, inject signal"],
        expected=[
            "Signal либо waits либо rejected (NOT silently approved)",
            "No order placed без safety check completing",
        ],
        recovery=["Release pool connections"],
    ),
    Scenario(
        id="X07",
        title="Time skew +30s между backend и daemon",
        layer="cross_layer",
        severity="S3",
        hypothesis="NTP drift не triggers false kill",
        pre_state=["FAKETIME='+30s' на backend container"],
        trigger=["Run daemon check_main_alive"],
        expected=[
            "Heartbeat freshness check tolerates 30s skew",
            "No false kill_switch SET",
        ],
        recovery=["Unset FAKETIME, restart backend"],
    ),
    Scenario(
        id="X08",
        title="Multi-cap simultaneous breach",
        layer="L2_risk_engine",
        severity="S3",
        hypothesis="per_bet + category + exposure все violated одновременно",
        pre_state=["bankroll=$1000, Sports cat=$290/$300, exposure=$390/$400"],
        trigger=["Inject Sports signal stake=$200"],
        expected=[
            "approved_stake reduced through cascade: per_bet ($50) → category ($10) → heat (...)",
            "reductions includes minimum: ['per_bet_cap', 'category_cap']",
            "Single TG alert (не дубли)",
        ],
        recovery=["fake_client.reset_state()"],
    ),
]

# --- Chaos scenarios (7, chaos engineering layer) -------------------------

CHAOS_SCENARIOS: list[Scenario] = [
    Scenario(
        id="C01",
        title="Postgres latency +2000ms 10 min",
        layer="chaos",
        severity="S2",
        hypothesis="asyncpg pool не насыщается, daemon queue не растёт >50",
        pre_state=["Toxiproxy proxy между backend и postgres"],
        trigger=["toxiproxy-cli add latency=2000 на postgres proxy 10 min"],
        expected=[
            "Pool active >= max_size briefly но <= 80%",
            "No connection timeout errors >5%",
            "Daemon check_interval не растёт >2× normal",
        ],
        recovery=["Remove Toxiproxy latency"],
    ),
    Scenario(
        id="C02",
        title="Redis SIGKILL посреди kill_switch",
        layer="chaos",
        severity="S1",
        hypothesis="После reboot Redis: kill_switch fires correctly от next tick",
        pre_state=["bot:kill_switch=SET"],
        trigger=["docker kill -9 oraclepoly-redis", "Wait 10s, restart"],
        expected=[
            "Daemon next check sees kill_switch gone (Redis empty after restart)",
            "Daemon re-evaluates, либо re-SET если cause persists",
            "Idempotent: no double SET",
        ],
        recovery=["redis-cli DEL bot:kill_switch"],
    ),
    Scenario(
        id="C03",
        title="Polymarket API 5xx 30% requests 1h",
        layer="chaos",
        severity="S1",
        hypothesis="execution_job retries с backoff, не дублирует ордера",
        pre_state=["FakeClobClient.set_failure_rate(0.3)"],
        trigger=["Run execution pipeline 1h"],
        expected=[
            "Retry counts: ≤5 attempts per order",
            "0 duplicate orders (idempotency UNIQUE)",
            "Failed orders → status='rejected' eventually",
        ],
        recovery=["fake_client.set_failure_rate(0.0)"],
    ),
    Scenario(
        id="C04",
        title="Clock skew +30s backend/daemon",
        layer="chaos",
        severity="S2",
        hypothesis="EIP-712 signatures валидны, no false-positive drift",
        pre_state=["FAKETIME='+30s' на backend"],
        trigger=["Run full execution + reconcile cycle"],
        expected=[
            "Signed orders accepted by Polymarket (Fake mocks success)",
            "No false drift alerts",
            "Audit logs use server time consistently",
        ],
        recovery=["Unset FAKETIME"],
    ),
    Scenario(
        id="C05",
        title="WebSocket forced disconnect каждые 90s 30min",
        layer="chaos",
        severity="S2",
        hypothesis="WSS reconnect, no message loss > 1",
        pre_state=["Toxiproxy на WSS port"],
        trigger=["reset_peer toxic с timeout=90s"],
        expected=[
            "Reconnection count = 20 (30min / 90s)",
            "Lost messages < 1% (per WSS log)",
            "Market state stays fresh (max staleness < 90s)",
        ],
        recovery=["Remove Toxiproxy"],
    ),
    Scenario(
        id="C06",
        title="Worker SIGTERM посреди batch",
        layer="chaos",
        severity="S2",
        hypothesis="In-flight task graceful-finishes ИЛИ idempotent retry",
        pre_state=["Worker processing 10-task batch"],
        trigger=["docker stop --time 10 oraclepoly-worker"],
        expected=[
            "Tasks fully processed либо retry on restart (idempotency)",
            "No duplicate detector_signals (UNIQUE constraint)",
        ],
        recovery=["docker start oraclepoly-worker"],
    ),
    Scenario(
        id="C07",
        title="Disk full в /app/logs",
        layer="chaos",
        severity="S3",
        hypothesis="App не падает hard, logs rotate или drop gracefully",
        pre_state=["dd if=/dev/zero of=/app/logs/fill bs=1M count=...filling disk"],
        trigger=["Wait for log writes hitting disk full"],
        expected=[
            "Backend stays up (not OOMKilled)",
            "Logs rotate (logrotate config triggers) либо drop with structured warning",
            "Alerting fires within 5min",
        ],
        recovery=["rm /app/logs/fill"],
    ),
]

ALL_SCENARIOS: list[Scenario] = FUNCTIONAL_SCENARIOS + CASCADE_SCENARIOS + CHAOS_SCENARIOS


def get_scenario(scenario_id: str) -> Scenario | None:
    """Lookup scenario by id (e.g. 'F01', 'X05', 'C03')."""
    for s in ALL_SCENARIOS:
        if s.id == scenario_id:
            return s
    return None


def scenarios_by_layer(layer: Layer) -> list[Scenario]:
    """Filter scenarios by layer (для targeted regression drills)."""
    return [s for s in ALL_SCENARIOS if s.layer == layer]


def scenarios_by_severity(severity: Severity) -> list[Scenario]:
    """Filter by severity (e.g. S1 only для critical-path quarterly drills)."""
    return [s for s in ALL_SCENARIOS if s.severity == severity]
