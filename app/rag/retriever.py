"""pgvector retrieval (FR-4.2, FR-4.3, FR-4.6)."""
from __future__ import annotations
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import settings
from app.state.store import store

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

CATEGORIES = {"service", "pricing", "case_study", "faq", "company", "process", "tech"}


@dataclass
class Hit:
    id: str
    content: str
    source_ref: str
    category: str
    similarity: float


async def embed(text: str) -> list[float]:
    resp = await _client.embeddings.create(model=settings.EMBEDDING_MODEL, input=[text])
    return resp.data[0].embedding


async def retrieve(query: str, *, top_k: int | None = None, category: str | None = None) -> list[Hit]:
    vector = await embed(query)
    literal = "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
    category = category if category in CATEGORIES else None
    async with store.pool.acquire() as con:
        rows = await con.fetch(
            "select * from match_chunks($1::extensions.vector, $2, $3, $4)",
            literal, top_k or settings.TOP_K, category, settings.MIN_SIMILARITY,
        )
    hits = [Hit(str(r["id"]), r["content"], r["source_ref"], r["category"], float(r["similarity"]))
            for r in rows]

    # FR-4.6: if the top hits are close in score but from different documents, keep them all -
    # the question spans more than one source document.
    if not hits:
        return []
    top = hits[0].similarity
    spread = [h for h in hits if top - h.similarity <= 0.05]
    if len({h.source_ref for h in spread}) > 1:
        return hits
    return hits[: max(3, top_k or settings.TOP_K)]
