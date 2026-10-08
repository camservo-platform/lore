from typing import Protocol

import httpx

from lore.settings import Settings


class Embedder(Protocol):
    async def embed_document(self, text: str) -> list[float]: ...
    async def embed_query(self, text: str) -> list[float]: ...


class OllamaEmbedder:
    """Embeds text with an Ollama server's /api/embed endpoint."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._client = client or httpx.AsyncClient(base_url=settings.embeddings_url, timeout=60)

    async def embed_document(self, text: str) -> list[float]:
        return await self._embed(self._settings.embeddings_document_prefix + text)

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(self._settings.embeddings_query_prefix + text)

    async def _embed(self, text: str) -> list[float]:
        resp = await self._client.post("/api/embed", json={"model": self._settings.embeddings_model, "input": text})
        resp.raise_for_status()
        vector = resp.json()["embeddings"][0]
        if len(vector) != self._settings.embeddings_dimensions:
            raise ValueError(
                f"{self._settings.embeddings_model} returned {len(vector)} dimensions, "
                f"expected {self._settings.embeddings_dimensions}"
            )
        return vector

    async def aclose(self) -> None:
        await self._client.aclose()
