"""
Embedding generation (V1 item #3).

First code in this project that actually calls Voyage. The columns
(`VECTOR(1024)`) and HNSW indexes have existed since the first schema and
have been unused until now.

Two deliberate choices:

  - `input_type` matters. Voyage embeds documents and queries into
    slightly different spaces on purpose; using "document" for stored
    nodes and "query" for search text measurably improves retrieval over
    using one for both. Getting this backwards degrades results quietly,
    with no error.

  - Failure is not silent. If embedding fails during seeding, the node is
    still written with a NULL embedding rather than the whole onboarding
    transaction aborting -- but the caller is told. A graph that
    half-embedded without anyone noticing would produce silently
    degraded search forever.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional, Sequence

from app.config import settings

log = logging.getLogger(__name__)

InputType = Literal["document", "query"]


class EmbeddingError(Exception):
    pass


class Embedder:
    """Thin wrapper over Voyage. Batches, because per-node calls are wasteful."""

    def __init__(self, model: Optional[str] = None, dimension: Optional[int] = None):
        self.model = model or settings.embedding_model
        self.dimension = dimension or settings.embedding_dimension

    async def embed(
        self, texts: Sequence[str], input_type: InputType = "document"
    ) -> list[list[float]]:
        if not texts:
            return []

        if settings.use_local_models:
            return await self._embed_local(texts)
        return await self._embed_voyage(texts, input_type)

    async def _embed_local(self, texts: Sequence[str]) -> list[list[float]]:
        """
        Any OpenAI-compatible embedding endpoint (Ollama, LM Studio).

        No `input_type` distinction: that's a Voyage-specific feature, and
        local models generally embed queries and documents into one space.
        Slightly worse retrieval, but not a correctness problem.
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key="not-needed-for-local", base_url=settings.local_base_url)
        try:
            result = await client.embeddings.create(
                model=settings.local_embedding_model, input=list(texts)
            )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"local embedding failed: {exc}") from exc

        vectors = [item.embedding for item in result.data]
        self._check_dimension(vectors, settings.local_embedding_model)
        return vectors

    async def _embed_voyage(
        self, texts: Sequence[str], input_type: InputType
    ) -> list[list[float]]:
        import voyageai

        client = voyageai.AsyncClient(api_key=settings.require("voyage_api_key"))
        try:
            result = await client.embed(
                list(texts), model=self.model, input_type=input_type
            )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"Voyage embedding failed: {exc}") from exc

        vectors = result.embeddings
        self._check_dimension(vectors, self.model)
        return vectors

    def _check_dimension(self, vectors: list[list[float]], model_name: str) -> None:
        """
        A dimension mismatch against the VECTOR(n) column fails at insert
        time with a far less obvious error, so catch it at the source.
        """
        if vectors and len(vectors[0]) != self.dimension:
            raise EmbeddingError(
                f"model {model_name} returned dimension {len(vectors[0])}, but the "
                f"schema expects {self.dimension}. Either pick a model with matching "
                f"dimension, or alter the VECTOR(n) column and re-embed the entire "
                f"corpus -- mixed dimensions in one column are not possible."
            )

    async def embed_one(self, text: str, input_type: InputType = "document") -> list[float]:
        vectors = await self.embed([text], input_type=input_type)
        if not vectors:
            raise EmbeddingError("no embedding returned")
        return vectors[0]


def to_pgvector(vector: Sequence[float]) -> str:
    """
    pgvector's text input format. asyncpg has no native codec for the
    vector type, so it goes over the wire as a string and gets cast in SQL.
    """
    return "[" + ",".join(str(float(v)) for v in vector) + "]"


def node_text(name: str, description: Optional[str] = None) -> str:
    """
    What actually gets embedded for a node.

    Name plus description, because a bare name ("Extract fields") carries
    much less signal than the same name with its purpose attached.
    """
    return f"{name}\n{description}" if description else name
