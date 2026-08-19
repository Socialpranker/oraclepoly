"""
Тесты LLM/Embeddings provider switching: OpenAI ↔ Cloudflare Workers AI.

Покрывает:
- Settings: CF-дефолты + env override (app.core.config)
- get_llm_client: выбор chat модели (app.core.llm)
- get_embedding_service: выбор embedding модели (app.core.embeddings)
- Fallback: cloudflare без CF_ACCOUNT_ID → openai

Объединяет бывшие test_llm_factory.py + test_embeddings_cf.py + test_config_cf.py.
"""

import os
from importlib import reload
from unittest.mock import MagicMock, patch


def _make_settings(**overrides):
    """Shared factory для патча app.core.config.get_settings.

    Полное множество параметров (llm + embeddings) чтобы один хелпер
    работал для тестов обоих модулей.
    """
    s = MagicMock()
    s.llm_provider = overrides.get("llm_provider", "openai")
    s.cf_account_id = overrides.get("cf_account_id", "")
    s.cf_api_token = overrides.get("cf_api_token", "")
    s.cf_model_chat = overrides.get("cf_model_chat", "@cf/meta/llama-3.1-8b-instruct")
    s.cf_model_embedding = overrides.get("cf_model_embedding", "@cf/baai/bge-large-en-v1.5")
    s.cf_embedding_dim = overrides.get("cf_embedding_dim", 1024)
    s.openrouter_api_key = overrides.get("openrouter_api_key", "")
    s.openrouter_embedding_model = overrides.get("openrouter_embedding_model", "openai/text-embedding-3-small")
    s.openrouter_embedding_dim = overrides.get("openrouter_embedding_dim", 1536)
    s.openai_api_key = overrides.get("openai_api_key", "sk-test")
    s.llm_analysis_model = overrides.get("llm_analysis_model", "gpt-4o-mini")
    s.llm_base_url = overrides.get("llm_base_url", None)
    s.llm_temperature = 0.4
    s.llm_max_tokens = 1500
    return s


# =====================================================================
# Settings: CF defaults + env override (app.core.config)
# (бывший test_config_cf.py)
# =====================================================================


def test_cf_settings_defaults(tmp_path, monkeypatch):
    """Проверяем CF-дефолты без .env файла (chdir в tmp_path где нет .env)."""
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "JWT_SECRET_KEY": "test-jwt-secret-key-32chars-or-more-for-validator",
    }
    with patch.dict(os.environ, env, clear=True):
        import app.core.config as cfg

        reload(cfg)
        cfg.get_settings.cache_clear()
        s = cfg.get_settings()
        assert s.cf_account_id == ""
        assert s.cf_api_token == ""
        assert s.cf_model_chat == "@cf/meta/llama-3.1-8b-instruct"
        assert s.cf_model_embedding == "@cf/baai/bge-large-en-v1.5"
        assert s.cf_embedding_dim == 1024


def test_cf_settings_from_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "JWT_SECRET_KEY": "test-jwt-secret-key-32chars-or-more-for-validator",
        "CF_ACCOUNT_ID": "abc123",
        "CF_API_TOKEN": "token456",
        "CF_MODEL_CHAT": "@cf/mistral/mistral-7b-instruct-v0.1",
        "LLM_PROVIDER": "cloudflare",
    }
    with patch.dict(os.environ, env, clear=True):
        import app.core.config as cfg

        reload(cfg)
        cfg.get_settings.cache_clear()
        s = cfg.get_settings()
        assert s.cf_account_id == "abc123"
        assert s.cf_api_token == "token456"
        assert s.cf_model_chat == "@cf/mistral/mistral-7b-instruct-v0.1"
        assert s.llm_provider == "cloudflare"


# =====================================================================
# LLM factory: OpenAI vs Cloudflare chat provider selection
# =====================================================================


def test_llm_factory_openai_default():
    settings = _make_settings()
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("langchain_openai.ChatOpenAI") as MockLLM:
            import app.core.llm as llm_mod

            reload(llm_mod)
            llm_mod.get_llm_client()
            kwargs = MockLLM.call_args[1]
            assert kwargs["model"] == "gpt-4o-mini"
            assert kwargs["api_key"] == "sk-test"


def test_llm_factory_cloudflare():
    settings = _make_settings(llm_provider="cloudflare", cf_account_id="acc123", cf_api_token="cf-token")
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("langchain_openai.ChatOpenAI") as MockLLM:
            import app.core.llm as llm_mod

            reload(llm_mod)
            llm_mod.get_llm_client()
            kwargs = MockLLM.call_args[1]
            assert kwargs["model"] == "@cf/meta/llama-3.1-8b-instruct"
            assert kwargs["api_key"] == "cf-token"
            assert "acc123" in kwargs["base_url"]
            assert "/ai/v1" in kwargs["base_url"]


def test_llm_factory_cf_fallback_to_openai():
    """cloudflare provider + пустой cf_account_id → fallback на openai."""
    settings = _make_settings(llm_provider="cloudflare", cf_account_id="")
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("langchain_openai.ChatOpenAI") as MockLLM:
            import app.core.llm as llm_mod

            reload(llm_mod)
            llm_mod.get_llm_client()
            kwargs = MockLLM.call_args[1]
            assert kwargs["model"] == "gpt-4o-mini"
            assert kwargs["api_key"] == "sk-test"


def test_llm_factory_json_mode():
    settings = _make_settings()
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("langchain_openai.ChatOpenAI") as MockLLM:
            import app.core.llm as llm_mod

            reload(llm_mod)
            llm_mod.get_llm_client(json_mode=True)
            kwargs = MockLLM.call_args[1]
            assert kwargs["model_kwargs"]["response_format"]["type"] == "json_object"


# =====================================================================
# Embeddings factory: OpenAI vs Cloudflare embedding provider
# (бывший test_embeddings_cf.py)
# =====================================================================


def test_embedding_openai_defaults():
    settings = _make_settings()
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("openai.AsyncOpenAI"):
            import app.core.embeddings as emb

            reload(emb)
            emb._embedding_service = None
            service = emb.get_embedding_service()
            assert service.model == "text-embedding-3-small"
            assert service.dimension == 1536


def test_embedding_cloudflare():
    settings = _make_settings(llm_provider="cloudflare", cf_account_id="acc123", cf_api_token="cf-token")
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("openai.AsyncOpenAI") as MockClient:
            import app.core.embeddings as emb

            reload(emb)
            emb._embedding_service = None
            service = emb.get_embedding_service()
            assert service.model == "@cf/baai/bge-large-en-v1.5"
            assert service.dimension == 1024
            call_kwargs = MockClient.call_args[1]
            assert "acc123" in call_kwargs["base_url"]


def test_embedding_openrouter():
    settings = _make_settings(llm_provider="openrouter", openrouter_api_key="sk-or-test")
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("openai.AsyncOpenAI") as MockClient:
            import app.core.embeddings as emb

            reload(emb)
            emb._embedding_service = None
            service = emb.get_embedding_service()
            assert service.model == "openai/text-embedding-3-small"
            assert service.dimension == 1536
            call_kwargs = MockClient.call_args[1]
            assert call_kwargs["api_key"] == "sk-or-test"
            assert "openrouter.ai/api/v1" in call_kwargs["base_url"]


def test_embedding_openrouter_fallback_to_openai():
    """openrouter provider + пустой openrouter_api_key → fallback на openai."""
    settings = _make_settings(llm_provider="openrouter", openrouter_api_key="")
    with patch("app.core.config.get_settings", return_value=settings):
        with patch("openai.AsyncOpenAI"):
            import app.core.embeddings as emb

            reload(emb)
            emb._embedding_service = None
            service = emb.get_embedding_service()
            assert service.model == "text-embedding-3-small"
            assert service.dimension == 1536
