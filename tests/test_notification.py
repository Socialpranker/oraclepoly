import json
from types import MappingProxyType

import pytest

from app.analytics.notification import ActionSpec, Notification, Severity


def _base(**over):
    kw = dict(
        severity=Severity.ACTION_REQUIRED,
        subject="detector:D38_resolution_lag",
        kind="edge_decay",
        headline="Преимущество детектора тает",
        what_happened=["Точность держится (~90% верных)."],
        what_if_ignored=None,
        what_to_do="Нажми 🔍 — отметить к разбору.",
        actions=[],
        evidence=["run #533"],
    )
    kw.update(over)
    return Notification(**kw)


def test_dedup_key_is_subject_and_kind():
    assert _base().dedup_key() == "detector:D38_resolution_lag|edge_decay"


def test_dedup_key_ignores_text_changes():
    """Ключ дедупа НЕ зависит от текста: иначе правка формулировки сбрасывает cooldown."""
    assert _base().dedup_key() == _base(headline="другой текст", evidence=["run #999"]).dedup_key()


def test_notification_is_frozen():
    n = _base()
    with pytest.raises(Exception):
        n.headline = "меняю"


def test_action_required_without_action_is_rejected():
    """ACTION_REQUIRED обязан нести следующий шаг, иначе он FYI (спека §4)."""
    with pytest.raises(ValueError, match="what_to_do"):
        _base(what_to_do=None)


def test_fyi_without_action_is_allowed():
    assert _base(severity=Severity.FYI, what_to_do=None).what_to_do is None


def test_what_happened_is_tuple_and_immutable():
    """Конструктор принимает list (удобство для джоб), но хранит tuple —
    append на нём бросает AttributeError, а не молча мутирует "замороженный" контракт."""
    n = _base(what_happened=["строка 1", "строка 2"])
    assert isinstance(n.what_happened, tuple)
    assert n.what_happened == ("строка 1", "строка 2")
    with pytest.raises(AttributeError):
        n.what_happened.append("подмена")


def test_actions_is_tuple_after_construction_from_list():
    action = ActionSpec(kind="requeue", payload={"id": 1}, snapshot={"score": 0.5})
    n = _base(actions=[action])
    assert isinstance(n.actions, tuple)
    assert n.actions == (action,)
    with pytest.raises(AttributeError):
        n.actions.append(action)


def test_evidence_is_tuple_after_construction_from_list():
    n = _base(evidence=["run #1", "run #2"])
    assert isinstance(n.evidence, tuple)
    with pytest.raises(AttributeError):
        n.evidence.append("run #3")


def test_action_spec_payload_is_read_only_mapping():
    action = ActionSpec(kind="requeue", payload={"id": 1}, snapshot={"score": 0.5})
    assert isinstance(action.payload, MappingProxyType)
    with pytest.raises(TypeError):
        action.payload["id"] = 2


def test_action_spec_snapshot_is_read_only_mapping():
    action = ActionSpec(kind="requeue", payload={"id": 1}, snapshot={"score": 0.5})
    assert isinstance(action.snapshot, MappingProxyType)
    with pytest.raises(TypeError):
        action.snapshot["score"] = 0.9


def test_action_spec_payload_json_dumps_via_dict_cast():
    """Контракт с action_gates.snapshot_hash(): вызывающий код обязан кастовать
    в dict перед json.dumps — сам MappingProxyType сериализуется иначе (см. docstring)."""
    action = ActionSpec(kind="requeue", payload={"b": 1, "a": 2}, snapshot={})
    serialized = json.dumps(dict(action.payload), sort_keys=True, default=str)
    assert serialized == '{"a": 2, "b": 1}'
