"""Тесты чистых функций-валидаторов Phase 0 seed-скрипта
(scripts/research/seed_agent_claims_phase0.py).

Task 3 — одноразовый сид, не incrementally-тестируемая логика вроде аудитов
Task 4-9, поэтому здесь только лёгкая sanity-проверка инвариантов, которые
и так проверяет acceptance брифа: 50 уникальных statement, валидные
scope/status/verify_policy, verify_halflife_days согласован с verify_policy,
не все клеймы дефолтно time_halflife. Никакой БД — `validate_claims`/
`count_by_verify_policy` чистые функции без side effects.
"""

from __future__ import annotations

from scripts.research.seed_agent_claims_phase0 import (
    CLAIMS,
    VALID_STATUSES,
    VALID_VERIFY_POLICIES,
    ClaimDict,
    count_by_verify_policy,
    validate_claims,
)


def _claim(**overrides: object) -> ClaimDict:
    base: ClaimDict = {
        "statement": "test statement",
        "scope": "detector:test",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 30,
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


class TestValidateClaimsOnRealList:
    """Инварианты брифа §Acceptance против реального CLAIMS-списка скрипта."""

    def test_exactly_fifty_claims(self) -> None:
        assert len(CLAIMS) == 50

    def test_no_validation_problems(self) -> None:
        assert validate_claims(CLAIMS) == []

    def test_all_statements_unique(self) -> None:
        statements = [c["statement"] for c in CLAIMS]
        assert len(statements) == len(set(statements))

    def test_all_scopes_non_empty(self) -> None:
        assert all(c["scope"].strip() for c in CLAIMS)

    def test_all_statuses_valid(self) -> None:
        assert all(c["status"] in VALID_STATUSES for c in CLAIMS)

    def test_all_verify_policies_valid(self) -> None:
        assert all(c["verify_policy"] in VALID_VERIFY_POLICIES for c in CLAIMS)

    def test_not_all_claims_default_to_time_halflife(self) -> None:
        """Acceptance: 'не все time_halflife по умолчанию — минимум риск-реестр
        и LLM-выбор помечены event_trigger'."""
        counts = count_by_verify_policy(CLAIMS)
        assert counts["event_trigger"] > 0
        assert counts["time_halflife"] > 0

    def test_risk_registry_claims_are_event_trigger(self) -> None:
        """UMA governance / MiCA — риск-реестр, должны быть event_trigger,
        не time_halflife (событийный триггер дешевле временного для рисков,
        см. Fable §2)."""
        risk_claims = [c for c in CLAIMS if c["scope"].startswith("risk:")]
        assert risk_claims, "expected at least one risk:* scoped claim"
        assert all(c["verify_policy"] == "event_trigger" for c in risk_claims)

    def test_llm_choice_claims_are_event_trigger(self) -> None:
        """LLM-провайдер/модель — версии инвалидируются событием (релиз новой
        модели), не временем."""
        llm_claims = [c for c in CLAIMS if c["scope"] == "policy:agent_llm_choice"]
        assert llm_claims, "expected at least one policy:agent_llm_choice scoped claim"
        assert all(c["verify_policy"] == "event_trigger" for c in llm_claims)


class TestValidateClaimsSyntheticCases:
    """Проверка самой функции validate_claims на сконструированных нарушениях."""

    def test_valid_single_claim_passes(self) -> None:
        assert validate_claims([_claim()]) == []

    def test_duplicate_statement_detected(self) -> None:
        problems = validate_claims([_claim(), _claim()])
        assert any("duplicate" in p for p in problems)

    def test_empty_statement_detected(self) -> None:
        problems = validate_claims([_claim(statement="")])
        assert any("empty statement" in p for p in problems)

    def test_empty_scope_detected(self) -> None:
        problems = validate_claims([_claim(scope="")])
        assert any("empty scope" in p for p in problems)

    def test_invalid_status_detected(self) -> None:
        problems = validate_claims([_claim(status="bogus")])
        assert any("invalid status" in p for p in problems)

    def test_invalid_verify_policy_detected(self) -> None:
        problems = validate_claims([_claim(verify_policy="bogus")])
        assert any("invalid verify_policy" in p for p in problems)

    def test_time_halflife_requires_non_null_days(self) -> None:
        problems = validate_claims([_claim(verify_policy="time_halflife", verify_halflife_days=None)])
        assert any("requires non-null verify_halflife_days" in p for p in problems)

    def test_event_trigger_allows_null_days(self) -> None:
        problems = validate_claims([_claim(verify_policy="event_trigger", verify_halflife_days=None)])
        assert problems == []

    def test_negative_halflife_days_detected(self) -> None:
        problems = validate_claims([_claim(verify_halflife_days=-1)])
        assert any("must be positive" in p for p in problems)

    def test_zero_halflife_days_detected(self) -> None:
        problems = validate_claims([_claim(verify_halflife_days=0)])
        assert any("must be positive" in p for p in problems)


class TestCountByVerifyPolicy:
    def test_empty_list(self) -> None:
        assert count_by_verify_policy([]) == {"time_halflife": 0, "event_trigger": 0}

    def test_mixed_list(self) -> None:
        claims = [
            _claim(statement="a", verify_policy="time_halflife", verify_halflife_days=30),
            _claim(statement="b", verify_policy="event_trigger", verify_halflife_days=None),
            _claim(statement="c", verify_policy="event_trigger", verify_halflife_days=None),
        ]
        assert count_by_verify_policy(claims) == {"time_halflife": 1, "event_trigger": 2}
