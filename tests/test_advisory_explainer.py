from unittest.mock import MagicMock

import pytest

from app.core.advisory_explainer import explain_advisory


@pytest.mark.asyncio
async def test_no_key_returns_none(monkeypatch):
    import app.core.advisory_explainer as mod

    spy = MagicMock()
    monkeypatch.setattr(
        mod, "get_settings", lambda: type("S", (), {"advisory_explainer_enabled": True, "openrouter_api_key": ""})()
    )
    monkeypatch.setattr(mod, "AsyncOpenAI", spy)
    result = await explain_advisory({"verdict": "REDUCE_RISK_REVIEW", "scope": "detector:D3"})
    assert result is None  # без ключа — молчим, без сетевого вызова
    spy.assert_not_called()  # клиент вообще не создавался — сетевой вызов физически невозможен


@pytest.mark.asyncio
async def test_llm_exception_returns_none(monkeypatch):
    import app.core.advisory_explainer as mod

    class FakeClient:
        def __init__(self, **kw):
            self.chat = self

        @property
        def completions(self):
            return self

        async def create(self, **kw):
            raise RuntimeError("boom")

    settings = type(
        "S",
        (),
        {
            "advisory_explainer_enabled": True,
            "openrouter_api_key": "sk-or-x",
            "advisory_explainer_model": "anthropic/claude-haiku-4.5",
            "advisory_explainer_max_tokens": 400,
        },
    )()
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(mod, "AsyncOpenAI", FakeClient)
    result = await explain_advisory({"verdict": "REDUCE_RISK_REVIEW", "scope": "detector:D3", "reason": "x"})
    assert result is None


@pytest.mark.asyncio
async def test_happy_path(monkeypatch):
    import app.core.advisory_explainer as mod

    class Msg:
        content = "Вердикт означает снижение риска по D3."

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    class FakeClient:
        def __init__(self, **kw):
            self.chat = self

        @property
        def completions(self):
            return self

        async def create(self, **kw):
            return Resp()

    settings = type(
        "S",
        (),
        {
            "advisory_explainer_enabled": True,
            "openrouter_api_key": "sk-or-x",
            "advisory_explainer_model": "anthropic/claude-haiku-4.5",
            "advisory_explainer_max_tokens": 400,
        },
    )()
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(mod, "AsyncOpenAI", FakeClient)
    result = await explain_advisory({"verdict": "REDUCE_RISK_REVIEW", "scope": "detector:D3", "reason": "roi -20%"})
    assert "риск" in result.lower()


@pytest.mark.asyncio
async def test_disabled_flag_returns_none_without_client(monkeypatch):
    import app.core.advisory_explainer as mod

    spy = MagicMock()
    settings = type(
        "S",
        (),
        {
            "advisory_explainer_enabled": False,
            "openrouter_api_key": "sk-or-present",  # ключ ЕСТЬ, но фича выключена
            "advisory_explainer_model": "anthropic/claude-haiku-4.5",
            "advisory_explainer_max_tokens": 400,
        },
    )()
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(mod, "AsyncOpenAI", spy)

    result = await explain_advisory({"verdict": "REDUCE_RISK_REVIEW", "scope": "detector:D3"})

    assert result is None
    spy.assert_not_called()  # клиент не создавался → наружу ничего не ушло
