"""Tests for app.jobs.position_manager — Active TP/SL management.

Покрываем чистую математику:
  - _unrealized_pnl_pct: правильная формула для YES/NO, edge cases.
  - _compute_early_exit_pnl: PnL при exit на промежуточной цене (не WIN/LOSS).
  - _thresholds_for: per-detector overrides + fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.jobs.position_manager import (
    DEFAULT_SL_PCT,
    DEFAULT_TP_PCT,
    MID_ZONE_CEILING,
    MID_ZONE_FLOOR,
    _compute_early_exit_pnl,
    _thresholds_for,
    _unrealized_pnl_pct,
)


def _note_text(call_arg) -> str:
    """Склеивает поля уведомления в текст — assert'ы по подстрокам остаются рабочими."""
    notes = call_arg if isinstance(call_arg, list) else [call_arg]
    parts = []
    for n in notes:
        parts += [n.headline, *n.what_happened, *(n.evidence or [])]
        parts += [n.what_to_do or "", n.what_if_ignored or ""]
    return " ".join(parts)


def _settings_with_virtual_exits_enabled():
    """Test-only Settings с virtual_early_exit_enabled=True — используется тестами,
    которые проверяют саму TP/SL/MZE-механику virtual-контура (по умолчанию она
    загейчена hold-to-resolution политикой, см. tests/test_position_manager_exit_parity.py)."""
    from app.core.config import Settings

    return Settings(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
        polymarket_api_secret="y",
        polymarket_api_passphrase="z",
        virtual_early_exit_enabled=True,
    )


# --- _unrealized_pnl_pct -------------------------------------------------


def test_unrealized_yes_break_even():
    """YES bought at 0.40, price still 0.40 → 0% PnL."""
    assert _unrealized_pnl_pct("YES", 0.40, 0.40) == pytest.approx(0.0, abs=0.001)


def test_unrealized_yes_doubled():
    """YES bought at 0.20, price now 0.40 → +100%."""
    assert _unrealized_pnl_pct("YES", 0.20, 0.40) == pytest.approx(100.0, abs=0.01)


def test_unrealized_yes_halved():
    """YES bought at 0.40, price now 0.20 → −50%."""
    assert _unrealized_pnl_pct("YES", 0.40, 0.20) == pytest.approx(-50.0, abs=0.01)


def test_unrealized_no_break_even():
    """NO entry fill=0.60 (=NO price = 1 − yes=0.40), now yes still 0.40 → 0%.

    Contract: fill_price = entry cost per share в координатах нашей стороны.
    Для NO side fill_price = NO_price at fill = 1 − YES_at_fill.
    NO position value per share = 1 − yes_now. Break-even если yes_now == yes_at_fill.
    """
    assert _unrealized_pnl_pct("NO", 0.60, 0.40) == pytest.approx(0.0, abs=0.001)


def test_unrealized_no_gain_when_yes_drops():
    """NO bought at fill=0.40 (yes_at_fill=0.60), now yes drops to 0.20.
    NO_now = 0.80. Return = 0.80/0.40 - 1 = +100%."""
    pnl = _unrealized_pnl_pct("NO", 0.40, 0.20)
    assert pnl == pytest.approx(100.0, abs=0.01)


def test_unrealized_no_loss_when_yes_rises():
    """NO bought at fill=0.9235 (yes_at_fill=0.0765), yes rises to 0.922 → NO crash.

    Реальная ситуация: NO entry cost 0.9235, current yes=0.922.
    NO_now = 1 - 0.922 = 0.078. Return = 0.078/0.9235 - 1 ≈ -91.55%.
    """
    pnl = _unrealized_pnl_pct("NO", 0.9235, 0.922)
    assert pnl == pytest.approx(-91.55, abs=0.5)


def test_unrealized_yes_zero_fill_safe():
    """YES @ fill_price=0 → None (degenerate)."""
    assert _unrealized_pnl_pct("YES", 0.0, 0.5) is None


def test_unrealized_no_zero_fill_safe():
    """NO @ fill_price=0 → None (degenerate)."""
    assert _unrealized_pnl_pct("NO", 0.0, 0.5) is None


def test_unrealized_unknown_side():
    """side='LONG' (typo) → None."""
    assert _unrealized_pnl_pct("LONG", 0.5, 0.5) is None


# --- _thresholds_for -----------------------------------------------------


def test_thresholds_default():
    """Unknown detector → defaults (mid-zone tuned, 2026-05-13)."""
    tp, sl = _thresholds_for("D999_unknown")
    assert tp == DEFAULT_TP_PCT
    assert sl == DEFAULT_SL_PCT
    # Mid-zone: TP=+30%, SL=-20%
    assert tp == 30.0
    assert sl == -20.0


def test_thresholds_d14_mid_zone():
    """D14 Crypto mid-zone winner — агрессивный 4h horizon."""
    tp, sl = _thresholds_for("D14_crypto_winners_distribute_no")
    assert tp == 25.0
    assert sl == -15.0


# --- 2026-05-19 TP/SL audit fixes ---------------------------------------


def test_thresholds_d3_hold_to_resolution_circuit_breaker():
    """D3_cross_mkt_arb — hold-to-resolution (human decision 2026-07-07 по
    exit-trajectories-audit-2026-07-06: зона старой политики TP+35/SL−15 хуже hold
    на 6-10 п.п./сигнал на всех haircut; SL порезал 21 будущего winner'а).
    TP/SL остаются только как catastrophic circuit-breaker, по образцу D38."""
    tp, sl = _thresholds_for("D3_cross_mkt_arb")
    assert tp == 200.0
    assert sl == -80.0


def test_thresholds_d36_obi_pressure_relaxed_sl():
    """D36_obi_pressure — actual WR 39% vs breakeven 41% (default). Relax SL → BE 35%."""
    tp, sl = _thresholds_for("D36_obi_pressure")
    assert tp == 30.0
    assert sl == -15.0


def test_thresholds_standalone_v2_lgb_mild_sl_relax():
    """STANDALONE_V2_LGB — actual WR 48% vs breakeven 52% (default). Mild SL tweak."""
    tp, sl = _thresholds_for("STANDALONE_V2_LGB")
    assert tp == 30.0
    assert sl == -18.0


# --- 2026-05-26 D38 hold-to-resolution -----------------------------------


def test_thresholds_d38_wide_circuit_breaker():
    """D38_resolution_lag — signal-level WR 89.8%, edge раскрывается only at resolution.
    TP отключён (200%), SL только catastrophic (-80%). Бизнес-логика выхода — HOLD_TO_RESOLUTION_DETECTORS."""
    tp, sl = _thresholds_for("D38_resolution_lag")
    assert tp == 200.0  # практически недостижимо — TP не нужен
    assert sl == -80.0  # circuit-breaker на model break


def test_d38_in_hold_to_resolution_set():
    """HOLD_TO_RESOLUTION_DETECTORS — single source of truth для skip MZE."""
    from app.jobs.position_manager import HOLD_TO_RESOLUTION_DETECTORS

    assert "D38_resolution_lag" in HOLD_TO_RESOLUTION_DETECTORS


# --- 2026-07-07 D3 hold-to-resolution (exit-trajectories-audit-2026-07-06) --


def test_d3_in_hold_to_resolution_set():
    """D3 переведён в hold-to-resolution (human decision 2026-07-07): MZE не
    выбрасывает арбитражные позиции, доходящие до экстремума за часы."""
    from app.jobs.position_manager import HOLD_TO_RESOLUTION_DETECTORS

    assert "D3_cross_mkt_arb" in HOLD_TO_RESOLUTION_DETECTORS


def test_d3_old_sl_zone_no_longer_exits():
    """Старая политика закрыла бы D3 по SL на −30% (порог был −15). Теперь держим:
    145 SL-выходов по всем детекторам закрыли будущих winners (D3: 21 случай)."""
    from app.jobs.position_manager import _decide_exit

    reason = _decide_exit(
        detector="D3_cross_mkt_arb",
        side="NO",
        pnl_pct=-30.0,
        current_price=0.60,
        age_minutes=120.0,  # SL-eligible age
        edge_pct=10.0,
    )
    assert reason is None, "−30% выше circuit-breaker −80 — держим до резолюции"


def test_d3_old_tp_zone_no_longer_exits():
    """Старая политика зафиксировала бы TP на +50% (порог был +35). Теперь держим:
    hold в гриде +46.6%/сигнал, вся зона TP+30..50 хуже hold."""
    from app.jobs.position_manager import _decide_exit

    reason = _decide_exit(
        detector="D3_cross_mkt_arb",
        side="YES",
        pnl_pct=50.0,
        current_price=0.60,
        age_minutes=120.0,
        edge_pct=10.0,
    )
    assert reason is None, "+50% ниже circuit-breaker TP 200 — держим до резолюции"


def test_d3_catastrophic_sl_still_fires():
    """Circuit-breaker −80% у D3 остаётся рабочим (model-break защита, как у D38)."""
    from app.jobs.position_manager import _decide_exit

    reason = _decide_exit(
        detector="D3_cross_mkt_arb",
        side="YES",
        pnl_pct=-85.0,
        current_price=0.10,
        age_minutes=120.0,
        edge_pct=10.0,
    )
    assert reason == "stop_loss"


# --- _compute_early_exit_pnl ---------------------------------------------


def test_early_exit_yes_at_50pct_gain():
    """YES bought at 0.40, exit at 0.60 → gross = stake × (0.60/0.40 - 1) = +50%."""
    res = _compute_early_exit_pnl(side="YES", stake=100.0, fill_price=0.40, exit_price=0.60, category="crypto")
    assert res["pnl"] == pytest.approx(50.0, abs=0.01)
    # Net должен быть меньше gross на fee×2 + slippage×2
    assert res["net_pnl"] < res["pnl"]
    # Но всё ещё положительный (50% gain - комиссии)
    assert res["net_pnl"] > 30.0


def test_early_exit_yes_at_30pct_loss():
    """YES bought at 0.40, exit at 0.28 → gross = -30%."""
    res = _compute_early_exit_pnl(side="YES", stake=100.0, fill_price=0.40, exit_price=0.28, category="crypto")
    assert res["pnl"] == pytest.approx(-30.0, abs=0.01)
    # Net хуже gross
    assert res["net_pnl"] < -30.0


def test_early_exit_no_save_from_disaster():
    """NO entry fill=0.9235 (yes_at_fill=0.0765), exit при yes=0.308.

    NO value: 0.9235 → (1-0.308)=0.692 → return = 0.692/0.9235 - 1 ≈ -25%.
    """
    res = _compute_early_exit_pnl(side="NO", stake=100.0, fill_price=0.9235, exit_price=0.308, category="politics")
    assert res["pnl"] == pytest.approx(-25.0, abs=0.5)
    assert res["net_pnl"] < res["pnl"]
    assert res["net_pnl"] > -35.0  # спасли от worst-case -100


def test_early_exit_no_take_profit():
    """NO entry fill=0.40 (yes_at_fill=0.60), exit когда yes drops to 0.20.
    NO value: 0.40 → 0.80 = +100% gain."""
    res = _compute_early_exit_pnl(side="NO", stake=100.0, fill_price=0.40, exit_price=0.20, category="sports")
    assert res["pnl"] == pytest.approx(100.0, abs=0.01)
    assert res["net_pnl"] > 80.0  # после fee+slippage все равно профит


def test_early_exit_no_break_even():
    """NO fill=0.30 (yes=0.70), exit when yes still 0.70 → 0% gross, net negative (costs).

    Slippage model (post-2026-05-18): 2 × shares × half_spread.
    shares = 100/0.30 = 333; crypto half_spread = 0.030 → slippage ≈ $20 + fees ≈ $3 → net ≈ −$23.
    """
    res = _compute_early_exit_pnl(side="NO", stake=100.0, fill_price=0.30, exit_price=0.70, category="crypto")
    assert res["pnl"] == pytest.approx(0.0, abs=0.01)
    assert res["net_pnl"] < 0  # только costs
    # Bound — round-trip slippage + fees на low fill price дороже чем для fill=0.5.
    # 2-leg slippage ≈ 2 × (stake/fill) × half_spread = 2 × 333 × 0.030 = $20.
    assert res["net_pnl"] > -30.0  # не катастрофа


def test_early_exit_no_invariant_qty_x_fill_equals_stake():
    """SSoT invariant: stake = shares × fill_price для NO side тоже.

    Это означает: shares = stake/fill, и при exit_yes=fill_yes_at_entry
    позиция точно break-even (без costs).
    """
    # fill_price = NO entry = 0.25 → yes_at_fill = 0.75
    res = _compute_early_exit_pnl(side="NO", stake=200.0, fill_price=0.25, exit_price=0.75, category="other")
    assert res["pnl"] == pytest.approx(0.0, abs=0.01)


def test_early_exit_unknown_side_returns_zeros():
    """side='HEDGE' (typo) → all zeros, не падаем."""
    res = _compute_early_exit_pnl(side="HEDGE", stake=100.0, fill_price=0.5, exit_price=0.5, category="crypto")
    assert res == {"pnl": 0.0, "trading_fee_usd": 0.0, "slippage_usd": 0.0, "net_pnl": 0.0}


def test_early_exit_fees_doubled_vs_settlement():
    """Early exit = round-trip (entry+exit fees), как WIN settlement.

    Slippage (post-2026-05-18): 2 × shares × half_spread.
    YES fill=0.40 → shares=250; crypto half_spread=0.030 → slippage ≈ $15 + fees ≈ $3 → net ≈ −$18.
    """
    # Для YES@0.40 exit@0.40 (break-even): gross=0, но fee×2 + slippage×2 платим
    res = _compute_early_exit_pnl(side="YES", stake=100.0, fill_price=0.40, exit_price=0.40, category="crypto")
    assert res["pnl"] == pytest.approx(0.0, abs=0.01)
    # Net должен быть отрицательным (только costs)
    assert res["net_pnl"] < 0
    # Bound — round-trip slippage shares-based: 2 × (100/0.40) × 0.030 = $15 + fees.
    assert res["net_pnl"] > -25.0


def test_early_exit_returns_floats_rounded():
    """Все значения float, округлены до 4 знаков."""
    res = _compute_early_exit_pnl(side="YES", stake=100.0, fill_price=0.40, exit_price=0.60, category="crypto")
    for key in ("pnl", "trading_fee_usd", "slippage_usd", "net_pnl"):
        assert isinstance(res[key], float)


# --- Mid-zone gate ------------------------------------------------------


def test_mid_zone_boundaries_match_user_intent():
    """User 2026-05-13: фокус на 0.50-0.70 predictions, не интересны 0.85+."""
    assert MID_ZONE_FLOOR == 0.30
    assert MID_ZONE_CEILING == 0.85


def test_mid_zone_floor_below_target_range():
    """Floor должен быть НИЖЕ user target range [0.50-0.70], чтобы дать room для drift."""
    assert MID_ZONE_FLOOR < 0.50


def test_mid_zone_ceiling_above_target_range():
    """Ceiling должен быть ВЫШЕ user target [0.50-0.70], чтобы позиция могла двинуться."""
    assert MID_ZONE_CEILING > 0.70


# --- SL age gate (MIN_AGE_FOR_STOP_LOSS_MINUTES) -------------------------
# Эмпирика 2026-05-18: SL <60мин в 27% случаев резал bы winners. Гейтим.


def test_sl_age_constant_blocks_first_hour():
    """MIN_AGE_FOR_STOP_LOSS_MINUTES должен быть >= 30 чтобы покрыть entry-noise."""
    from app.jobs.position_manager import (
        MIN_AGE_FOR_STOP_LOSS_MINUTES,
        MIN_POSITION_AGE_MINUTES,
    )

    assert MIN_AGE_FOR_STOP_LOSS_MINUTES >= 30
    # SL гейт должен быть строже общего age gate (TP/MZE).
    assert MIN_AGE_FOR_STOP_LOSS_MINUTES > MIN_POSITION_AGE_MINUTES


@pytest.mark.asyncio
async def test_run_position_manager_skips_young_sl(monkeypatch):
    """Позиция с SL trigger но age < 60мин → skip, не закрываем."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    # YES @ 0.40, current 0.30 → -25% PnL → SL trigger (default -20% threshold).
    # Но позиция filled только 15 минут назад → должна быть held.
    young_row = {
        "id": "test-young-sl",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xabc",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,  # not high-edge
        "category": "sports",
        "current_price": 0.31,  # -22.5% drawdown — SL trigger (mid zone, not MZE)
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [young_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)

    result = await pm.run_position_manager()

    assert result["sl_closed"] == 0, "young SL should not fire"
    assert result["sl_held_young"] == 1, "expected exactly 1 held young SL"
    assert captured_updates == [], "no UPDATE should happen for held young SL"


@pytest.mark.asyncio
async def test_run_position_manager_fires_old_sl(monkeypatch):
    """Та же позиция с возрастом > 60мин → SL срабатывает нормально."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    old_row = {
        "id": "test-old-sl",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xabc",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=90),  # >60мин
        "edge_pct": 10.0,
        "category": "sports",
        "current_price": 0.31,  # -22.5% drawdown — SL trigger (mid zone, not MZE)
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [old_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", _settings_with_virtual_exits_enabled)

    result = await pm.run_position_manager()

    assert result["sl_closed"] == 1, "old SL should fire"
    assert result["sl_held_young"] == 0
    assert len(captured_updates) == 1
    # Первый позиционный аргумент UPDATE — exit_reason, должен быть "stop_loss".
    assert captured_updates[0][1] == "stop_loss"


@pytest.mark.asyncio
async def test_d38_position_in_extreme_zone_NOT_closed_by_mze(monkeypatch):
    """D38 hold-to-resolution: позиция при current_price=0.95 (в extreme zone)
    НЕ должна быть закрыта по mid_zone_exit. RCA 2026-05-26 H1."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    # D38 NO entry fill=0.05 (yes_at_fill=0.95). Current yes стал 0.95 — это
    # in extreme zone (>= 0.85). До fix'а MZE закрыл бы. После fix — держим.
    d38_row = {
        "id": "test-d38-mze-skip",
        "detector": "D38_resolution_lag",
        "market_id": "0xd38",
        "side": "NO",
        "fill_price": 0.05,  # NO entry cost
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=120),  # SL eligible
        "edge_pct": 10.0,  # не high-edge — проверяем именно hold_to_resolution
        "category": "sports",
        "current_price": 0.95,  # in extreme zone
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [d38_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)

    result = await pm.run_position_manager()

    # NO @ fill 0.05, current yes=0.95 → NO_now = 0.05 → 0% PnL — не TP не SL.
    # MZE гейт должен пропустить D38 даже без high-edge.
    assert result["mid_zone_exits"] == 0, "D38 в hold-to-resolution не должен MZE"
    assert result["high_edge_kept"] == 1, "счётчик kept должен инкрементиться"


@pytest.mark.asyncio
async def test_d38_catastrophic_sl_still_fires(monkeypatch):
    """D38 circuit-breaker: SL=-80% всё ещё работает при катастрофическом drift."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    # D38 YES fill=0.50, current=0.05 → -90% drawdown — должен trigger SL -80%.
    d38_disaster = {
        "id": "test-d38-disaster",
        "detector": "D38_resolution_lag",
        "market_id": "0xd38",
        "side": "YES",
        "fill_price": 0.50,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=120),
        "edge_pct": 10.0,
        "category": "sports",
        "current_price": 0.05,  # extreme zone + -90% PnL
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [d38_disaster]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", _settings_with_virtual_exits_enabled)

    result = await pm.run_position_manager()

    # MZE пропущен (hold_to_resolution), но SL -80% сработал → high_edge_kept++ + sl_closed.
    assert result["sl_closed"] == 1, "circuit-breaker SL должен сработать"
    assert result["mid_zone_exits"] == 0
    assert captured_updates[0][1] == "stop_loss"


# --- TP-realism: exit-on-extreme guard (вариант A, 2026-06-05) ------------
# Диагноз: position_manager закрывал по take_profit, когда current_price УЖЕ на
# резолюционном экстремуме (≤0.02 / ≥0.98) — "продажа по mid" по цене резолюции,
# для которой нет контрагента (NO-share не продать по ~$1.0 когда YES выиграл).
# Это маскированная резолюция, записанная как TP. Фантом локализован: STANDALONE
# 6 сделок +$5520 (avg +920%/≤20мин), D3 3 сделки +$1186. Honest STANDALONE
# +$3311 − фантом = −$2208 (убыточен). Fix (вариант A): на экстремуме НЕ срабатывать
# TP — держать позицию до настоящей резолюции (settlement, 1 нога). SL/MZE НЕ
# трогаем (circuit-breaker нужен; SL-убыток на экстремуме реалистичен). См. memory
# standalone_virtual_pnl_inflation_2026-06-04 §6.3.


def test_resolution_extreme_thresholds_defined():
    """Порог экстремума определён и уже резолюционных значений (жёстче mid-zone)."""
    from app.jobs.position_manager import (
        PHANTOM_TP_MAX_HOLD_MINUTES,
        RESOLUTION_EXTREME_CEILING,
        RESOLUTION_EXTREME_FLOOR,
    )

    assert 0.0 < RESOLUTION_EXTREME_FLOOR < MID_ZONE_FLOOR, "floor жёстче mid-zone floor"
    assert MID_ZONE_CEILING < RESOLUTION_EXTREME_CEILING < 1.0, "ceiling жёстче mid-zone ceiling"
    assert PHANTOM_TP_MAX_HOLD_MINUTES > 0, "порог hold для фантом-TP определён"


def test_is_phantom_tp_exit_predicate():
    """Чистый предикат: extreme + short-hold = фантом; extreme + long-hold = нет."""
    from app.jobs.position_manager import _is_phantom_tp_exit

    # extreme low + short hold → фантом
    assert _is_phantom_tp_exit(0.001, age_minutes=15) is True
    # extreme high + short hold → фантом
    assert _is_phantom_tp_exit(0.999, age_minutes=15) is True
    # extreme + LONG hold → НЕ фантом (D3 арбитражное схождение за часы)
    assert _is_phantom_tp_exit(0.005, age_minutes=130) is False
    # не extreme + short hold → НЕ фантом (нормальный TP)
    assert _is_phantom_tp_exit(0.50, age_minutes=15) is False


@pytest.mark.asyncio
async def test_tp_NOT_fired_at_resolution_extreme_high(monkeypatch):
    """STANDALONE NO @ fill 0.19, current_yes=0.001 → NO_now≈0.999 = +426% PnL.
    Это TP-триггер, НО current_price на экстремуме (≤0.02) → фантом. Держим.

    edge_pct=420 (high-edge) → MZE bypass → попадаем в TP-ветку, как реальный
    фантом на проде (все 6 фантомных TP имели ev_pct 90-979, см. O1)."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    phantom_row = {
        "id": "test-phantom-tp-low",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xphantom",
        "side": "NO",
        "fill_price": 0.19,  # NO entry cost
        "stake": 250.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),  # короткий hold
        "edge_pct": 420.0,  # high-edge → MZE bypass → TP-ветка (как фантом на проде)
        "category": "crypto",
        "current_price": 0.001,  # YES рухнул к нулю → рынок фактически разрешился
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [phantom_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 0, "TP на резолюционном экстремуме = фантом, не закрывать"
    assert result["tp_held_extreme"] == 1, "счётчик фантом-guard должен инкрементиться"
    assert captured_updates == [], "никакого UPDATE — позиция держится до резолюции"


@pytest.mark.asyncio
async def test_tp_NOT_fired_at_resolution_extreme_ceiling(monkeypatch):
    """YES @ fill 0.073, current_yes=0.999 → +1268% PnL. TP-триггер, но экстремум
    (≥0.98) → фантом (это был реальный +$3159 артефакт на проде). Держим."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    phantom_row = {
        "id": "test-phantom-tp-high",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xphantom2",
        "side": "YES",
        "fill_price": 0.073,
        "stake": 250.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 979.0,  # high-edge → MZE bypass → TP-ветка (реальный +$3159 артефакт)
        "category": "crypto",
        "current_price": 0.999,  # YES почти разрешился в 1.0
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [phantom_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 0, "TP на экстремуме (≥0.98) = фантом"
    assert captured_updates == []


@pytest.mark.asyncio
async def test_tp_STILL_fires_at_normal_price(monkeypatch):
    """Регрессия: нормальный TP (current_price НЕ на экстремуме) работает как раньше.
    NO @ fill 0.50, current_yes=0.32 → NO_now=0.68 = +36% PnL ≥ TP. Закрываем.

    edge_pct=60 (high-edge) → MZE bypass, чтобы изолированно проверить TP-ветку.
    current 0.32 НЕ в extreme (>0.02) и НЕ в mid-zone-floor (>0.30) → чистый TP."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    normal_row = {
        "id": "test-normal-tp",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xnormal",
        "side": "NO",
        "fill_price": 0.50,
        "stake": 250.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 60.0,  # high-edge → MZE bypass → изолируем TP-ветку
        "category": "crypto",
        "current_price": 0.32,  # NO_now = 0.68 = +36% TP; не экстремум, не mid-floor
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [normal_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", _settings_with_virtual_exits_enabled)

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 1, "нормальный TP должен сработать"
    assert captured_updates[0][1] == "take_profit"


@pytest.mark.asyncio
async def test_tp_STILL_fires_at_extreme_with_LONG_hold(monkeypatch):
    """Фантом-guard НЕ ломает легитимный long-hold TP: extreme exit + LONG hold
    (130мин) = реальное схождение к резолюции за часы, НЕ фантом. TP должен сработать.

    NO @ fill 0.563, current_yes=0.02 → NO_now=0.98 = +74% за 130мин.
    (Исторически тест был на D3 — с 2026-07-07 D3 hold-to-resolution и TP у него
    circuit-breaker 200%; guard-механику проверяем на STANDALONE_V2_LGB, tp=30.)"""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    arb_row = {
        "id": "test-d3-longhold-arb",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xd3arb",
        "side": "NO",
        "fill_price": 0.563,  # NO entry cost
        "stake": 250.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=130),  # LONG hold
        "edge_pct": 80.0,  # high-edge → MZE bypass → TP-ветка
        "category": "crypto",
        "current_price": 0.02,  # extreme, НО дошёл за 130мин = легит схождение
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [arb_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", _settings_with_virtual_exits_enabled)

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 1, "long-hold extreme TP = легит арбитраж, должен сработать"
    assert result["tp_held_extreme"] == 0, "не должен попасть под фантом-guard (long hold)"
    assert captured_updates[0][1] == "take_profit"


@pytest.mark.asyncio
async def test_sl_STILL_fires_at_resolution_extreme(monkeypatch):
    """SL на экстремуме НЕ блокируется (только TP). YES @ fill 0.50, current=0.01
    → -98% → catastrophic SL должен сработать (circuit-breaker, убыток реалистичен).

    edge_pct=60 (high-edge) → MZE bypass → попадаем в SL-ветку (а не MZE), чтобы
    проверить именно: SL на экстремуме НЕ глушится фиксом (фикс трогает только TP)."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    sl_row = {
        "id": "test-sl-extreme",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xslextreme",
        "side": "YES",
        "fill_price": 0.50,
        "stake": 250.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=120),  # SL eligible
        "edge_pct": 60.0,  # high-edge → MZE bypass → SL-ветка
        "category": "crypto",
        "current_price": 0.01,  # экстремум + катастрофический убыток
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [sl_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", _settings_with_virtual_exits_enabled)

    result = await pm.run_position_manager()

    assert result["sl_closed"] == 1, "SL на экстремуме должен сработать (не блокируем SL)"
    assert captured_updates[0][1] == "stop_loss"


# --- Live exit path (audit топ-1, 2026-07-02) ----------------------------
# _DETECTOR_OVERRIDES расширен до троек (tp_pct, sl_pct, max_hold_hours | None).
# _thresholds_for остаётся 2-tuple (backward compat) — max_hold через _max_hold_for.


def test_thresholds_for_still_returns_2_tuple():
    """_thresholds_for не меняет сигнатуру после расширения _DETECTOR_OVERRIDES до троек."""
    result = _thresholds_for("D3_cross_mkt_arb")
    assert len(result) == 2
    tp, sl = result
    assert tp == 200.0
    assert sl == -80.0


def test_max_hold_for_default_is_none():
    """Detector без override (и default) → max_hold_hours=None (нет max_hold триггера)."""
    from app.jobs.position_manager import _max_hold_for

    assert _max_hold_for("D999_unknown") is None
    assert _max_hold_for("D3_cross_mkt_arb") is None  # существующие overrides получили None 3-м элементом


def test_detector_overrides_are_triples():
    """_DETECTOR_OVERRIDES: каждая запись — (tp, sl, max_hold_hours|None)."""
    from app.jobs.position_manager import _DETECTOR_OVERRIDES

    for detector, entry in _DETECTOR_OVERRIDES.items():
        assert len(entry) == 3, f"{detector}: ожидается тройка (tp, sl, max_hold)"
        tp, sl, max_hold = entry
        assert isinstance(tp, float)
        assert isinstance(sl, float)
        assert max_hold is None or isinstance(max_hold, float)


# --- _decide_exit (общая exit-логика virtual + live) ----------------------


def test_decide_exit_none_when_pnl_pct_is_none():
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(detector="D999", side="YES", pnl_pct=None, current_price=0.5, age_minutes=100, edge_pct=0.0)
    assert result is None


def test_decide_exit_take_profit_normal():
    from app.jobs.position_manager import _decide_exit

    # default TP=+30%, current не extreme/mid-zone-floor
    result = _decide_exit(detector="D999", side="YES", pnl_pct=40.0, current_price=0.55, age_minutes=15, edge_pct=10.0)
    assert result == "take_profit"


def test_decide_exit_stop_loss_after_age_gate():
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(detector="D999", side="YES", pnl_pct=-25.0, current_price=0.35, age_minutes=90, edge_pct=10.0)
    assert result == "stop_loss"


def test_decide_exit_stop_loss_blocked_when_young():
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(detector="D999", side="YES", pnl_pct=-25.0, current_price=0.35, age_minutes=15, edge_pct=10.0)
    assert result is None


def test_decide_exit_mid_zone_exit():
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(detector="D999", side="YES", pnl_pct=5.0, current_price=0.90, age_minutes=15, edge_pct=10.0)
    assert result == "mid_zone_exit"


def test_decide_exit_max_hold_triggers_when_no_other_reason():
    """max_hold_hours=8, age=9h, no TP/SL trigger → max_hold."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999",
        side="YES",
        pnl_pct=5.0,
        current_price=0.50,  # mid-zone, не triggers MZE
        age_minutes=9 * 60,
        edge_pct=10.0,
        max_hold_hours=8.0,
    )
    assert result == "max_hold"


def test_decide_exit_max_hold_does_not_override_earlier_trigger():
    """TP уже сработал раньше max_hold — exit_reason остаётся take_profit, не max_hold."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999",
        side="YES",
        pnl_pct=40.0,
        current_price=0.55,
        age_minutes=9 * 60,  # старше max_hold, но TP уже сработал бы первым
        edge_pct=10.0,
        max_hold_hours=8.0,
    )
    assert result == "take_profit"


def test_decide_exit_max_hold_none_never_triggers():
    """max_hold_hours=None (дефолт для существующих overrides) → нет max_hold триггера
    даже при очень старой позиции без TP/SL."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999",
        side="YES",
        pnl_pct=5.0,
        current_price=0.50,
        age_minutes=100 * 60,  # 100h — очень старая
        edge_pct=10.0,
        max_hold_hours=None,
    )
    assert result is None


def test_decide_exit_max_hold_not_yet_reached():
    """age < max_hold_hours → не триггерит max_hold."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999",
        side="YES",
        pnl_pct=5.0,
        current_price=0.50,
        age_minutes=7 * 60,  # 7h < max_hold 8h
        edge_pct=10.0,
        max_hold_hours=8.0,
    )
    assert result is None


def test_decide_exit_take_profit_skipped_when_price_stale():
    """is_live=False (WS протух, fallback на 15-минутный db_price) → TP не
    защитный механизм, при неопределённой цене НЕ действуем (skip)."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999", side="YES", pnl_pct=40.0, current_price=0.55, age_minutes=15, edge_pct=10.0, is_live=False
    )
    assert result is None


def test_decide_exit_stop_loss_not_skipped_when_price_stale():
    """is_live=False НЕ блокирует SL — асимметрия рисков: пропустить реальный
    обвал на 15 минут дороже, чем чуть ранний защитный выход."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999", side="YES", pnl_pct=-25.0, current_price=0.35, age_minutes=90, edge_pct=10.0, is_live=False
    )
    assert result == "stop_loss"


def test_decide_exit_mid_zone_exit_skipped_when_stale():
    """mid_zone_exit трактуется как TP-подобный (не защитный) → skip при stale."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(
        detector="D999", side="YES", pnl_pct=5.0, current_price=0.90, age_minutes=15, edge_pct=10.0, is_live=False
    )
    assert result is None


def test_decide_exit_default_is_live_true_preserves_existing_behavior():
    """Дефолт is_live=True не меняет поведение — обратная совместимость для
    virtual-пути, который не передаёт этот параметр вообще."""
    from app.jobs.position_manager import _decide_exit

    result = _decide_exit(detector="D999", side="YES", pnl_pct=40.0, current_price=0.55, age_minutes=15, edge_pct=10.0)
    assert result == "take_profit"


# --- run_live_position_manager --------------------------------------------


@pytest.mark.asyncio
async def test_run_live_position_manager_no_open_positions(monkeypatch):
    from app.jobs import position_manager as pm

    async def mock_fetch(_sql, _arg):
        return []

    monkeypatch.setattr(pm, "fetch", mock_fetch)

    result = await pm.run_live_position_manager()

    assert result == {"checked": 0, "exit_requested": 0, "exit_failed": 0, "skipped": 0, "skipped_stale_price": 0}


@pytest.mark.asyncio
async def test_run_live_position_manager_requests_exit_on_tp(monkeypatch):
    """Live position с TP-триггером → close_live_position вызван (НЕ paper-UPDATE)."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    live_row = {
        "id": "live-order-1",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xlive",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.60,  # +50% → TP trigger (STANDALONE override tp=30)
        "market_resolved": False,
        "clob_token_ids": None,
    }

    async def mock_fetch(_sql, _arg):
        return [live_row]

    async def mock_get_live_yes_price(_token_id, db_price):
        return db_price, True  # свежая цена — тестируем TP-логику, не staleness

    close_calls = []

    async def mock_close_live_position(order_id, reason):
        close_calls.append((order_id, reason))
        return True, "sell-oid-live"

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr("app.services.live_prices.get_live_yes_price", mock_get_live_yes_price)
    monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)

    result = await pm.run_live_position_manager()

    assert result["exit_requested"] == 1
    assert result["exit_failed"] == 0
    assert len(close_calls) == 1
    assert close_calls[0] == ("live-order-1", "take_profit")


@pytest.mark.asyncio
async def test_run_live_position_manager_skips_when_no_exit_reason(monkeypatch):
    """Live position без TP/SL/MZE/max_hold trigger → skip, close_live_position НЕ вызван."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    live_row = {
        "id": "live-order-2",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xlive2",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.41,  # ~+2.5% — не TP, не SL, не extreme
        "market_resolved": False,
        "clob_token_ids": None,
    }

    async def mock_fetch(_sql, _arg):
        return [live_row]

    async def mock_get_live_yes_price(_token_id, db_price):
        return db_price, True  # свежая цена — тестируем обычный skip, не staleness

    close_calls = []

    async def mock_close_live_position(order_id, reason):
        close_calls.append((order_id, reason))
        return True, "sell-oid"

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr("app.services.live_prices.get_live_yes_price", mock_get_live_yes_price)
    monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)

    result = await pm.run_live_position_manager()

    assert result["skipped"] == 1
    assert result["exit_requested"] == 0
    assert close_calls == []


@pytest.mark.asyncio
async def test_run_live_position_manager_counts_exit_failure(monkeypatch):
    """close_live_position вернул ok=False (напр. no_book_for_exit) → exit_failed++, не крашится."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    live_row = {
        "id": "live-order-3",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xlive3",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=90),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.30,  # -25% → SL trigger (age eligible, >60min)
        "market_resolved": False,
        "clob_token_ids": None,
    }

    async def mock_fetch(_sql, _arg):
        return [live_row]

    async def mock_get_live_yes_price(_token_id, db_price):
        return db_price, True  # свежая цена — тестируем failure-путь, не staleness

    async def mock_close_live_position(order_id, reason):
        return False, "no_book_for_exit"

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr("app.services.live_prices.get_live_yes_price", mock_get_live_yes_price)
    monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)
    # Шина мокается явно: она пишет в БД, а пула в юнит-тестах нет.
    monkeypatch.setattr(pm, "publish", AsyncMock())

    result = await pm.run_live_position_manager()

    assert result["exit_failed"] == 1
    assert result["exit_requested"] == 0


@pytest.mark.asyncio
async def test_run_live_position_manager_stale_price_skips_tp(monkeypatch):
    """get_live_yes_price возвращает is_live=False (WS протух, fallback db_price)
    → TP-подобное решение skip'ается, close_live_position НЕ вызван,
    summary['skipped_stale_price'] отражает это отдельно от обычного skipped."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    live_row = {
        "id": "live-order-stale",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xlivestale",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.60,  # +50% → был бы TP trigger, если бы цена была живой
        "market_resolved": False,
        "clob_token_ids": None,
    }

    async def mock_fetch(_sql, _arg):
        return [live_row]

    async def mock_get_live_yes_price(_token_id, db_price):
        return db_price, False  # stale — WS протух

    close_calls = []

    async def mock_close_live_position(order_id, reason):
        close_calls.append((order_id, reason))
        return True, "sell-oid"

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr("app.services.live_prices.get_live_yes_price", mock_get_live_yes_price)
    monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)

    result = await pm.run_live_position_manager()

    assert result["skipped_stale_price"] == 1
    assert result["exit_requested"] == 0
    assert close_calls == []


@pytest.mark.asyncio
async def test_run_position_manager_skips_live_contour_when_flag_disabled(monkeypatch):
    """settings.live_exit_enabled=False (default) → run_position_manager НЕ вызывает
    run_live_position_manager (гейт до human-решения + ramp)."""
    from app.jobs import position_manager as pm

    async def mock_fetch(_sql, _arg):
        return []

    live_contour_called = []

    async def mock_run_live_position_manager():
        live_contour_called.append(True)
        return {"checked": 0}

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "run_live_position_manager", mock_run_live_position_manager)

    result = await pm.run_position_manager()

    assert live_contour_called == [], "live_exit_enabled default False — live-контур не должен запускаться"
    assert "live" not in result


@pytest.mark.asyncio
async def test_run_position_manager_runs_live_contour_when_flag_enabled(monkeypatch):
    """settings.live_exit_enabled=True → run_position_manager вызывает live-контур
    и подмешивает его summary в result['live']."""
    from app.core.config import Settings
    from app.jobs import position_manager as pm

    async def mock_fetch(_sql, _arg):
        return []

    live_contour_called = []

    async def mock_run_live_position_manager():
        live_contour_called.append(True)
        return {"checked": 0, "exit_requested": 0, "exit_failed": 0, "skipped": 0}

    def _make_settings_live_exit_enabled():
        return Settings(
            database_url="postgresql://x:x@localhost/x",
            openai_api_key="sk-x",
            redis_url="redis://localhost",
            telegram_bot_token="1:x",
            admin_telegram_ids=[1],
            admin_api_key="x",
            polymarket_api_key="x",
            polymarket_api_secret="y",
            polymarket_api_passphrase="z",
            live_exit_enabled=True,
        )

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "run_live_position_manager", mock_run_live_position_manager)
    monkeypatch.setattr("app.core.config.get_settings", _make_settings_live_exit_enabled)

    result = await pm.run_position_manager()

    assert live_contour_called == [True]
    assert result["live"] == {"checked": 0, "exit_requested": 0, "exit_failed": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_maybe_run_live_contour_alerts_on_exception(monkeypatch):
    """run_live_position_manager кидает исключение → TG-алерт (не только тихий logger.error).

    Regression: раньше exception только логировался — live-контур мог не работать
    циклами подряд без единого сигнала админу (см. инцидент с зависшими позициями)."""
    from app.core.config import Settings
    from app.jobs import position_manager as pm

    async def mock_run_live_position_manager():
        raise RuntimeError("db connection lost")

    def _make_settings_live_exit_enabled():
        return Settings(
            database_url="postgresql://x:x@localhost/x",
            openai_api_key="sk-x",
            redis_url="redis://localhost",
            telegram_bot_token="1:x",
            admin_telegram_ids=[1],
            admin_api_key="x",
            polymarket_api_key="x",
            polymarket_api_secret="y",
            polymarket_api_passphrase="z",
            live_exit_enabled=True,
        )

    mock_notify = AsyncMock()
    monkeypatch.setattr(pm, "run_live_position_manager", mock_run_live_position_manager)
    monkeypatch.setattr("app.core.config.get_settings", _make_settings_live_exit_enabled)
    monkeypatch.setattr("app.jobs.position_manager.publish", mock_notify)

    summary: dict = {}
    await pm._maybe_run_live_contour(summary)

    assert "live" not in summary
    mock_notify.assert_called_once()
    assert "управление live-позициями упало" in _note_text(mock_notify.call_args[0][0]).lower()
    assert "db connection lost" in _note_text(mock_notify.call_args[0][0])


@pytest.mark.asyncio
async def test_run_live_position_manager_exit_failure_alerts(monkeypatch):
    """close_live_position вернул ok=False → TG-алерт с order_id/detector/reason,
    не только logger.warning (иначе SELL-фейл невидим админу, позиция висит)."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    live_row = {
        "id": "live-order-alert",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xlivealert",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=90),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.30,
        "market_resolved": False,
        "clob_token_ids": None,
    }

    async def mock_fetch(_sql, _arg):
        return [live_row]

    async def mock_get_live_yes_price(_token_id, db_price):
        return db_price, True

    async def mock_close_live_position(order_id, reason):
        return False, "no_book_for_exit"

    mock_notify = AsyncMock()
    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr("app.services.live_prices.get_live_yes_price", mock_get_live_yes_price)
    monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)
    monkeypatch.setattr("app.jobs.position_manager.publish", mock_notify)

    result = await pm.run_live_position_manager()

    assert result["exit_failed"] == 1
    mock_notify.assert_called_once()
    msg = _note_text(mock_notify.call_args[0][0])
    assert "live-order-alert" in msg
    assert "no_book_for_exit" in msg


@pytest.mark.asyncio
async def test_failed_exits_get_per_order_subjects(monkeypatch):
    """Две неудачные попытки выхода → два уведомления с РАЗНЫМИ subject.

    С общим subject вторая застрявшая позиция подавилась бы cooldown'ом
    первой, и про её деньги владелец бы не узнал. Мутация «общий subject»
    раньше проходила незамеченной — этот тест её и ловит.
    """
    from datetime import UTC, datetime, timedelta

    from app.jobs import position_manager as pm

    def _row(order_id: str, market: str):
        return {
            "id": order_id,
            "detector": "STANDALONE_V2_LGB",
            "market_id": market,
            "side": "YES",
            "fill_price": 0.40,
            "stake": 100.0,
            "filled_at": datetime.now(UTC) - timedelta(minutes=90),
            "edge_pct": 10.0,
            "category": "crypto",
            "current_price": 0.30,  # -25% → SL
            "market_resolved": False,
            "clob_token_ids": None,
        }

    async def mock_fetch(_sql, _arg):
        return [_row("live-a", "0xaaa"), _row("live-b", "0xbbb")]

    async def mock_get_live_yes_price(_token_id, db_price):
        return db_price, True

    async def mock_close_live_position(order_id, reason):
        return False, "no_book_for_exit"

    published: list = []

    async def mock_publish(notes, actor=None):
        published.extend(notes)
        return "PublishResult(sent=1)"

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr("app.services.live_prices.get_live_yes_price", mock_get_live_yes_price)
    monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)
    monkeypatch.setattr(pm, "publish", mock_publish)

    result = await pm.run_live_position_manager()

    assert result["exit_failed"] == 2
    subjects = {n.subject for n in published}
    assert subjects == {"order:live-a", "order:live-b"}, f"subject должен быть свой у каждой позиции: {subjects}"
