"""Тесты пяти гейтов L2-исполнения (Phase 3). Чистый слой — без I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from app.analytics.action_gates import (
    ALLOWED_KINDS,
    KIND_PAUSE_DETECTOR,
    KIND_REDUCE_CAP,
    ActionRequest,
    check_gates,
    snapshot_hash,
)

_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_SNAP = {"stage": "live"}


def _req(**kw) -> ActionRequest:
    base = dict(
        kind=KIND_PAUSE_DETECTOR,
        status="proposed",
        expires_at=_NOW + timedelta(hours=1),
        payload_hash=snapshot_hash(_SNAP),
        is_admin=True,
    )
    base.update(kw)
    return ActionRequest(**base)


def test_all_gates_pass():
    d = check_gates(_req(), _NOW, "GREEN", _SNAP)
    assert d.allowed is True
    assert d.reason is None


def test_non_admin_rejected():
    d = check_gates(_req(is_admin=False), _NOW, "GREEN", _SNAP)
    assert d.allowed is False
    assert d.reason == "not_admin"


def test_expired_ttl_rejected_fail_closed():
    """auto-approve запрещён (спека §2 п.4): истёк — значит не выполняется."""
    d = check_gates(_req(expires_at=_NOW - timedelta(seconds=1)), _NOW, "GREEN", _SNAP)
    assert d.allowed is False
    assert d.reason == "expired"


def test_already_executed_rejected():
    d = check_gates(_req(status="executed"), _NOW, "GREEN", _SNAP)
    assert d.allowed is False
    assert d.reason == "already_handled"


def test_snapshot_mismatch_rejected():
    """Данные изменились с момента отправки — исполнять по устаревшей картине нельзя."""
    d = check_gates(_req(), _NOW, "GREEN", {"stage": "paused"})
    assert d.allowed is False
    assert d.reason == "snapshot_changed"


def test_dq_red_rejected():
    d = check_gates(_req(), _NOW, "RED", _SNAP)
    assert d.allowed is False
    assert d.reason == "dq_red"


def test_dq_amber_allowed():
    """AMBER не блокирует — блокирует только RED (симметрично policy_engine)."""
    d = check_gates(_req(), _NOW, "AMBER", _SNAP)
    assert d.allowed is True


def test_unknown_kind_rejected():
    """Whitelist: неизвестный kind отклоняется ДО адаптера (спека §8 риск 3)."""
    d = check_gates(_req(kind="rm_rf_everything"), _NOW, "GREEN", _SNAP)
    assert d.allowed is False
    assert d.reason == "unknown_kind"


def test_admin_gate_wins_over_other_failures():
    """Приоритет отказов: не-админ важнее всего остального — не сообщаем чужому о состоянии."""
    d = check_gates(
        _req(is_admin=False, status="executed", expires_at=_NOW - timedelta(hours=99)),
        _NOW,
        "RED",
        {"stage": "other"},
    )
    assert d.reason == "not_admin"


def test_snapshot_hash_is_order_independent():
    """Порядок ключей не меняет хэш — иначе ложные отказы на ровном месте."""
    assert snapshot_hash({"a": 1, "b": 2}) == snapshot_hash({"b": 2, "a": 1})


def test_snapshot_hash_differs_on_value_change():
    assert snapshot_hash({"cap": 10.0}) != snapshot_hash({"cap": 5.0})


def test_snapshot_hash_same_for_dict_and_mapping_proxy():
    """MappingProxyType (ActionSpec хранит payload/snapshot именно так) обязан давать
    ТОТ ЖЕ хэш, что и dict с теми же данными.

    Без dict() внутри `default=str` сериализовал бы proxy как repr-строку без сортировки
    ключей — тихо другой хэш на тех же данных. В проде это отклоняло бы КАЖДОЕ исполнение
    с текстом «данные изменились», а моки в тестах шины такого не увидели бы.
    """
    data = {"stage": "shadow", "live_stake_cap_usd": 10.0}
    assert snapshot_hash(MappingProxyType(data)) == snapshot_hash(data)


def test_snapshot_hash_mapping_proxy_is_order_independent():
    """Порядок ключей не меняет хэш и для proxy — сортировка не теряется при нормализации."""
    assert snapshot_hash(MappingProxyType({"a": 1, "b": 2})) == snapshot_hash(MappingProxyType({"b": 2, "a": 1}))


def test_allowed_kinds_is_exactly_two():
    """Whitelist не расширяется молча (спека §2 п.3).

    close_position сюда НЕ входит намеренно: у него нет потребителя (policy engine
    не знает order_db_id). Необратимое действие не получает разрешения «на будущее».
    """
    assert ALLOWED_KINDS == frozenset({KIND_PAUSE_DETECTOR, KIND_REDUCE_CAP})
