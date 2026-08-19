"""Policy engine — чистый слой правил (Phase 2 C1).

Детерминированный: входы (датаклассы) → список Proposal. Никакого I/O, никакого LLM.
Спека: docs/superpowers/specs/2026-07-23-portfolio-agent-phase2-policy-engine-design.md §4-§5.
Пороги versioned в policy_rules (rule_key='policy_engine'); _DEFAULT_PARAMS — fallback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.analytics.ips import INACTIVE_IPS_GATE, IpsGate

VERDICT_OBSERVE = "OBSERVE"
VERDICT_INVESTIGATE_DATA = "INVESTIGATE_DATA"
VERDICT_KEEP_SHADOW = "KEEP_SHADOW"
VERDICT_PAUSE_REVIEW = "PAUSE_REVIEW"
VERDICT_REDUCE_RISK_REVIEW = "REDUCE_RISK_REVIEW"
VERDICT_CLOSE_POSITION_REVIEW = "CLOSE_POSITION_REVIEW"
VERDICT_START_VIRTUAL_REVIEW = "START_VIRTUAL_REVIEW"
VERDICT_LIVE_RAMP_REVIEW = "LIVE_RAMP_REVIEW"

VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_OBSERVE,
        VERDICT_INVESTIGATE_DATA,
        VERDICT_KEEP_SHADOW,
        VERDICT_PAUSE_REVIEW,
        VERDICT_REDUCE_RISK_REVIEW,
        VERDICT_CLOSE_POSITION_REVIEW,
        VERDICT_START_VIRTUAL_REVIEW,
        VERDICT_LIVE_RAMP_REVIEW,
    }
)

# Вердикты, блокируемые при DQ RED (спека §4: «DQ RED → блок risk-вердиктов цикла»)
RISK_VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_KEEP_SHADOW,
        VERDICT_PAUSE_REVIEW,
        VERDICT_REDUCE_RISK_REVIEW,
        VERDICT_CLOSE_POSITION_REVIEW,
    }
)

# Пороги v1 — решение владельца 2026-07-23 (прод-baseline: D3 live ROI −33%/30d).
_DEFAULT_PARAMS: dict = {
    "drawdown_reduce_roi": -0.15,
    "drawdown_close_roi": -0.30,
    "drawdown_min_settled": 20,
    "drawdown_window_days": 30,
    # 25, а не 24: джоба суточная (cron 07:30), интервал между прогонами ровно 24.00ч —
    # окно в 24ч попадает на границу и срабатывает через раз по джиттеру (контрфактика
    # на 17 исторических proposals: 6ч → 0 подавлений, 24ч → 8, 25ч → 12).
    "cooldown_hours": 25,
}


@dataclass(frozen=True)
class PolicyThresholds:
    drawdown_reduce_roi: float
    drawdown_close_roi: float
    drawdown_min_settled: int
    drawdown_window_days: int
    cooldown_hours: int

    @classmethod
    def from_params(cls, params: dict) -> PolicyThresholds:
        return cls(
            drawdown_reduce_roi=float(params["drawdown_reduce_roi"]),
            drawdown_close_roi=float(params["drawdown_close_roi"]),
            drawdown_min_settled=int(params["drawdown_min_settled"]),
            drawdown_window_days=int(params["drawdown_window_days"]),
            cooldown_hours=int(params["cooldown_hours"]),
        )


@dataclass(frozen=True)
class DqCheckLine:
    """Одна проверка C0 из agent_runs.metrics.checks[] — только то, что нужно тексту.

    Осознанно НЕ переиспользуем dq_audit.DqCheckResult: он несёт measured/amber/red,
    которые в advisory не выводятся (спека §3 п.2), а импорт связал бы два независимых
    чистых слоя ради трёх полей.
    """

    check: str  # "freshness:markets"
    verdict: str  # GREEN | AMBER | RED
    detail: str  # "markets: возраст последней записи 2713 мин"


@dataclass(frozen=True)
class DqStatus:
    """Свежий вердикт C0 DQ-аудита (GREEN/AMBER/RED); run_id=None — DQ-run не найден.

    checks — не-пустой только когда джоба смогла разобрать metrics.checks[];
    дефолт () держит обратную совместимость двухаргументных вызовов.
    """

    verdict: str
    run_id: int | None
    checks: tuple[DqCheckLine, ...] = ()


@dataclass(frozen=True)
class MonitorSignal:
    """Один run монитора Phase 1 (edge_decay / adverse_selection)."""

    monitor: str  # "edge_decay" | "adverse_selection"
    entity: str  # имя детектора (совпадает с detector_state.detector)
    category: str | None  # только у adverse_selection (detector×category)
    verdict: str  # "decaying"/"stable" | "adverse"/"clean"
    run_id: int
    stage: str | None  # detector_state.stage: shadow|virtual|live|paused; None — детектор без строки


@dataclass(frozen=True)
class LiveDrawdown:
    """Bankroll-weighted live-агрегат по детектору (SUM net_pnl / SUM stake — НЕ sum-per-signal)."""

    detector: str
    n_settled: int
    net_pnl: float
    stake: float

    @property
    def roi(self) -> float | None:
        if self.stake == 0:
            return None
        return self.net_pnl / self.stake


@dataclass(frozen=True)
class Proposal:
    verdict: str
    scope: str  # конвенция "type:value", напр. "detector:D3_cross_mkt_arb"
    reason: str
    evidence_run_ids: tuple[int, ...] = field(default_factory=tuple)
    rule_version: int = 0
    blocked: bool = False  # True — вердикт заблокирован DQ RED (эмитится как маркер, не как риск)
    blocked_by: str | None = None  # "DQ_RED" | "IPS_VIOLATION" — причина блокировки (для statement клеймов)


def proposal_id(verdict: str, scope: str, rule_version: int, window: str) -> str:
    """Дедуп-ключ proposal (спека §5): hash(verdict, scope, rule_version, окно)."""
    raw = f"{verdict}|{scope}|{rule_version}|{window}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _monitor_proposals(signals: list[MonitorSignal], rule_version: int) -> list[Proposal]:
    out: list[Proposal] = []
    for s in signals:
        if s.monitor == "edge_decay" and s.verdict == "decaying":
            if s.stage == "live":
                verdict = VERDICT_REDUCE_RISK_REVIEW
            else:
                # virtual/shadow/paused/None — консервативно PAUSE_REVIEW (спека §4: virtual-строка)
                verdict = VERDICT_PAUSE_REVIEW
            out.append(
                Proposal(
                    verdict=verdict,
                    scope=f"detector:{s.entity}",
                    reason=f"edge decay (stage={s.stage or 'unknown'})",
                    evidence_run_ids=(s.run_id,),
                    rule_version=rule_version,
                )
            )
        elif s.monitor == "adverse_selection" and s.verdict == "adverse":
            if s.stage == "live":
                out.append(
                    Proposal(
                        verdict=VERDICT_REDUCE_RISK_REVIEW,
                        scope=f"detector:{s.entity}:category:{s.category}",
                        reason=f"adverse selection in {s.category} (live)",
                        evidence_run_ids=(s.run_id,),
                        rule_version=rule_version,
                    )
                )
            else:
                out.append(
                    Proposal(
                        verdict=VERDICT_KEEP_SHADOW,
                        scope=f"detector:{s.entity}",
                        reason=f"adverse selection in {s.category} (block promote)",
                        evidence_run_ids=(s.run_id,),
                        rule_version=rule_version,
                    )
                )
    return out


def _drawdown_proposals(
    drawdowns: list[LiveDrawdown], thresholds: PolicyThresholds, rule_version: int
) -> list[Proposal]:
    out: list[Proposal] = []
    for dd in drawdowns:
        roi = dd.roi
        if roi is None or dd.n_settled < thresholds.drawdown_min_settled:
            continue
        if roi < thresholds.drawdown_close_roi:
            verdict = VERDICT_CLOSE_POSITION_REVIEW
        elif roi < thresholds.drawdown_reduce_roi:
            verdict = VERDICT_REDUCE_RISK_REVIEW
        else:
            continue
        out.append(
            Proposal(
                verdict=verdict,
                scope=f"detector:{dd.detector}",
                reason=(
                    f"live drawdown: net ROI {roi:.1%} over {thresholds.drawdown_window_days}d "
                    f"(net ${dd.net_pnl:.2f} / stake ${dd.stake:.2f}, n={dd.n_settled})"
                ),
                rule_version=rule_version,
            )
        )
    return out


def _format_dq_reason(dq: DqStatus) -> str:
    """Причина DQ-предложения: сработавшие проверки построчно, иначе честный фолбэк.

    Формат строки повторяет 🟡-алерт C0 (dq_audit_daily._format_alert) — одно
    сообщение об одном инциденте не должно выглядеть по-разному. Порогов нет (спека §3 п.2).
    Фолбэк не врёт: он не называет причин, но и не выдумывает их (спека §3 п.4).
    """
    failed = [c for c in dq.checks if c.verdict != "GREEN"]
    if not failed:
        suffix = "DQ-run не найден" if dq.run_id is None else "детали проверок недоступны"
        return f"DQ audit verdict {dq.verdict} ({suffix})"
    lines = [f"DQ {dq.verdict} — сработали проверки:"]
    lines.extend(f"• {c.check}: {c.verdict} — {c.detail}" for c in failed)
    return "\n".join(lines)


def evaluate(
    dq: DqStatus,
    signals: list[MonitorSignal],
    drawdowns: list[LiveDrawdown],
    thresholds: PolicyThresholds,
    rule_version: int,
    ips_gate: IpsGate = INACTIVE_IPS_GATE,
) -> list[Proposal]:
    """Все 6 правил §4 + IPS-гейт §2 п.8. Всегда ≥1 Proposal. DQ RED или IPS violation → risk-вердикты
    blocked=True (blocked_by = причина)."""
    proposals: list[Proposal] = []

    if dq.verdict in ("AMBER", "RED"):
        proposals.append(
            Proposal(
                verdict=VERDICT_INVESTIGATE_DATA,
                scope="policy:dq_audit",
                reason=_format_dq_reason(dq),
                evidence_run_ids=(dq.run_id,) if dq.run_id is not None else (),
                rule_version=rule_version,
            )
        )

    if ips_gate.violated:
        # IPS_VIOLATION — блок-механизм (спека §2 п.8), НЕ значение enum §4.2.
        proposals.append(
            Proposal(
                verdict=VERDICT_INVESTIGATE_DATA,
                scope="policy:ips",
                reason="IPS violation: " + "; ".join(ips_gate.violations),
                rule_version=rule_version,
            )
        )

    risk = _monitor_proposals(signals, rule_version) + _drawdown_proposals(drawdowns, thresholds, rule_version)

    # DQ RED первичен: при ненадёжных данных и IPS-вывод ненадёжен.
    blocked_by = "DQ_RED" if dq.verdict == "RED" else ("IPS_VIOLATION" if ips_gate.violated else None)
    if blocked_by is not None:
        # Спека §4/§5 + §2 п.8: блок risk-вердиктов цикла; джоба переведёт их
        # в BLOCKED_DATA-клеймы вместо proposals.
        risk = [
            Proposal(
                verdict=p.verdict,
                scope=p.scope,
                reason=p.reason,
                evidence_run_ids=p.evidence_run_ids,
                rule_version=p.rule_version,
                blocked=True,
                blocked_by=blocked_by,
            )
            for p in risk
        ]

    proposals.extend(risk)

    if not proposals:
        proposals.append(
            Proposal(
                verdict=VERDICT_OBSERVE,
                scope="portfolio:all",
                reason="all rules clean",
                rule_version=rule_version,
            )
        )
    return proposals
