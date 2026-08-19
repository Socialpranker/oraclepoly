"""
Embedding Generation Service

Поддерживает OpenAI (text-embedding-3-small, 1536d)
и Cloudflare Workers AI (BGE-large-en-v1.5, 1024d).
"""

from openai import AsyncOpenAI

from app.core.config import get_settings

_OPENAI_MODEL = "text-embedding-3-small"
_OPENAI_DIM = 1536

# OpenRouter отдаёт 200 OK с пустым data на слишком большие батчи
# ("No embedding data received" из SDK) — режем вход на чанки.
EMBED_BATCH_CHUNK_SIZE = 500


class EmbeddingService:
    """
    Service for generating text embeddings.

    Провайдер определяется из Settings:
    - openai (default): text-embedding-3-small (1536d)
    - cloudflare: CF BGE-large-en-v1.5 (1024d, OpenAI-совместимый API)
    """

    def __init__(self):
        settings = get_settings()
        use_cloudflare = settings.llm_provider == "cloudflare" and bool(settings.cf_account_id)
        use_openrouter = settings.llm_provider == "openrouter" and bool(settings.openrouter_api_key)

        if use_cloudflare:
            self.model = settings.cf_model_embedding
            self.dimension = settings.cf_embedding_dim
            base_url = f"https://api.cloudflare.com/client/v4/accounts/{settings.cf_account_id}/ai/v1"
            self.client = AsyncOpenAI(api_key=settings.cf_api_token, base_url=base_url)
        elif use_openrouter:
            # OpenRouter — OpenAI-совместимый шлюз (POST /api/v1/embeddings).
            self.model = settings.openrouter_embedding_model
            self.dimension = settings.openrouter_embedding_dim
            self.client = AsyncOpenAI(api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1")
        else:
            self.model = _OPENAI_MODEL
            self.dimension = _OPENAI_DIM
            self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def embed_text(self, text: str) -> list[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed (will be truncated if too long)

        Returns:
            List of floats representing the embedding vector
        """
        # Truncate to ~8000 tokens (rough char estimate)
        if len(text) > 25000:
            text = text[:25000]

        response = await self.client.embeddings.create(
            model=self.model,
            input=text,
        )

        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        More efficient than calling embed_text in a loop.
        Вход режется на чанки по EMBED_BATCH_CHUNK_SIZE текстов,
        результаты склеиваются в исходном порядке.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors

        Raises:
            ValueError: провайдер вернул не столько embeddings, сколько текстов отправлено
        """
        if not texts:
            return []

        # Truncate each text
        truncated = [t[:25000] if len(t) > 25000 else t for t in texts]

        embeddings: list[list[float]] = []
        for start in range(0, len(truncated), EMBED_BATCH_CHUNK_SIZE):
            chunk = truncated[start : start + EMBED_BATCH_CHUNK_SIZE]
            response = await self.client.embeddings.create(
                model=self.model,
                input=chunk,
            )
            if len(response.data) != len(chunk):
                raise ValueError(
                    f"Embedding batch size mismatch: sent {len(chunk)} texts, "
                    f"received {len(response.data)} embeddings (chunk offset {start})"
                )
            # Sort by index to maintain order within the chunk
            sorted_data = sorted(response.data, key=lambda x: x.index)
            embeddings.extend(item.embedding for item in sorted_data)

        return embeddings


# Singleton instance
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get or create the embedding service instance."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
