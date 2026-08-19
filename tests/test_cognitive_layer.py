"""
Phase 2: Cognitive Layer Tests

Tests for embeddings.
Uses mock mode to avoid external API calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    mock = MagicMock()
    mock.openai_api_key = "test-openai-key"
    mock.qdrant_url = "https://test.qdrant.io"
    mock.qdrant_api_key = "test-qdrant-key"
    mock.firecrawl_api_key = "test-firecrawl-key"
    mock.database_url = "postgresql://test:test@localhost:5432/test"
    mock.r2_bucket_name = "test-bucket"
    mock.r2_endpoint_url = "https://test.r2.cloudflarestorage.com"
    mock.r2_access_key_id = "test-key"
    mock.r2_secret_access_key = "test-secret"
    mock.llm_provider = "openai"
    mock.cf_account_id = ""
    mock.cf_api_token = ""
    mock.cf_model_embedding = "@cf/baai/bge-large-en-v1.5"
    mock.cf_embedding_dim = 1024
    with patch("app.core.config.get_settings", return_value=mock):
        with patch("app.core.embeddings.get_settings", return_value=mock):
            yield mock


class TestEmbeddingService:
    """Tests for OpenAI embedding generation."""

    @pytest.mark.asyncio
    async def test_embed_text(self, mock_settings):
        """Test single text embedding."""
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 1536)]

        with patch("app.core.embeddings.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.embeddings.create = AsyncMock(return_value=mock_response)
            mock_openai.return_value = mock_client

            from app.core.embeddings import EmbeddingService

            service = EmbeddingService()

            result = await service.embed_text("Bitcoin price rises")

            assert len(result) == 1536
            mock_client.embeddings.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_text_truncation(self, mock_settings):
        """Test that long texts are truncated."""
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 1536)]

        with patch("app.core.embeddings.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.embeddings.create = AsyncMock(return_value=mock_response)
            mock_openai.return_value = mock_client

            from app.core.embeddings import EmbeddingService

            service = EmbeddingService()

            # Create very long text
            long_text = "A" * 30000
            await service.embed_text(long_text)

            # Check the call was made with truncated text
            call_args = mock_client.embeddings.create.call_args
            assert len(call_args.kwargs["input"]) <= 25000
