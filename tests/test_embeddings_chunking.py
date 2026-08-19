"""Чанкинг embed_batch — фикс "No embedding data received" (OpenRouter лимит батча).

Диагностика: research/manifold-mapper-diagnostics-2026-07-09/answer.md.
Canonical-батч (~3245 текстов) уходил одним запросом → OpenRouter отдаёт
200 OK с пустым data → OpenAI SDK бросает ValueError("No embedding data received").
Фикс: embed_batch режет вход на чанки ≤ EMBED_BATCH_CHUNK_SIZE и склеивает результат.

Все тесты — моки, без реального API.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_settings():
    """Settings для openai-провайдера (без CF/OpenRouter)."""
    mock = MagicMock()
    mock.openai_api_key = "test-openai-key"
    mock.llm_provider = "openai"
    mock.cf_account_id = ""
    mock.cf_api_token = ""
    with patch("app.core.embeddings.get_settings", return_value=mock):
        yield mock


def _make_client(create_side_effect):
    client = AsyncMock()
    client.embeddings.create = AsyncMock(side_effect=create_side_effect)
    return client


def _echo_response(input_texts: list[str], reverse: bool = False):
    """Response, где embedding кодирует исходный текст: "text-7" → [7.0].

    reverse=True возвращает data в обратном порядке индексов — проверка
    что merge сортирует по index внутри чанка.
    """
    items = []
    for i, text in enumerate(input_texts):
        item = MagicMock()
        item.index = i
        item.embedding = [float(text.split("-")[1])]
        items.append(item)
    if reverse:
        items = list(reversed(items))
    response = MagicMock()
    response.data = items
    return response


@pytest.mark.asyncio
async def test_embed_batch_chunks_large_input(mock_settings):
    """Батч больше EMBED_BATCH_CHUNK_SIZE режется на несколько запросов, порядок сохранён."""

    async def create(model, input):
        return _echo_response(input, reverse=True)

    client = _make_client(create)
    with patch("app.core.embeddings.AsyncOpenAI", return_value=client):
        from app.core.embeddings import EMBED_BATCH_CHUNK_SIZE, EmbeddingService

        service = EmbeddingService()
        n_texts = EMBED_BATCH_CHUNK_SIZE * 2 + 201  # 1201 при chunk=500
        texts = [f"text-{i}" for i in range(n_texts)]

        result = await service.embed_batch(texts)

        assert len(result) == n_texts
        # Порядок соответствует входу несмотря на reverse внутри чанков
        assert result == [[float(i)] for i in range(n_texts)]
        # 1201 текст при чанке 500 → 3 запроса (500 + 500 + 201)
        assert client.embeddings.create.call_count == 3
        sent_sizes = [len(c.kwargs["input"]) for c in client.embeddings.create.call_args_list]
        assert sent_sizes == [EMBED_BATCH_CHUNK_SIZE, EMBED_BATCH_CHUNK_SIZE, 201]


@pytest.mark.asyncio
async def test_embed_batch_small_input_single_request(mock_settings):
    """Батч ≤ chunk size уходит одним запросом (поведение не меняется)."""

    async def create(model, input):
        return _echo_response(input)

    client = _make_client(create)
    with patch("app.core.embeddings.AsyncOpenAI", return_value=client):
        from app.core.embeddings import EmbeddingService

        service = EmbeddingService()
        result = await service.embed_batch([f"text-{i}" for i in range(3)])

        assert result == [[0.0], [1.0], [2.0]]
        assert client.embeddings.create.call_count == 1


@pytest.mark.asyncio
async def test_embed_batch_empty_input_no_api_call(mock_settings):
    """Пустой вход → [] без обращения к API."""
    client = _make_client(None)
    with patch("app.core.embeddings.AsyncOpenAI", return_value=client):
        from app.core.embeddings import EmbeddingService

        service = EmbeddingService()
        result = await service.embed_batch([])

        assert result == []
        client.embeddings.create.assert_not_called()


@pytest.mark.asyncio
async def test_embed_batch_raises_on_response_size_mismatch(mock_settings):
    """Ответ короче запроса → явный ValueError с размерами (не молчаливая потеря)."""

    async def create(model, input):
        return _echo_response(input[:-1])  # провайдер "потерял" один embedding

    client = _make_client(create)
    with patch("app.core.embeddings.AsyncOpenAI", return_value=client):
        from app.core.embeddings import EmbeddingService

        service = EmbeddingService()
        texts = [f"text-{i}" for i in range(5)]

        with pytest.raises(ValueError, match=r"sent 5.*received 4"):
            await service.embed_batch(texts)


@pytest.mark.asyncio
async def test_embed_batch_truncates_long_texts(mock_settings):
    """Существующее поведение: тексты >25000 символов усекаются."""

    async def create(model, input):
        response = MagicMock()
        response.data = [MagicMock(index=i, embedding=[0.0]) for i in range(len(input))]
        return response

    client = _make_client(create)
    with patch("app.core.embeddings.AsyncOpenAI", return_value=client):
        from app.core.embeddings import EmbeddingService

        service = EmbeddingService()
        await service.embed_batch(["A" * 30000])

        sent = client.embeddings.create.call_args.kwargs["input"]
        assert len(sent[0]) == 25000
