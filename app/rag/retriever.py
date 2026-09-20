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

    # FR-4.6: every hit above the similarity floor is returned, so a question spanning two source
    # documents gets chunks from both. match_chunks' own LIMIT (top_k, default 6) is the only cap.
    # The Search agent logs `multi_source` when the surviving citations name more than one document.
    #
    # This used to branch: if the close hits (within 0.05 of the top) came from different documents
    # it returned `hits`, otherwise `hits[: max(3, top_k or TOP_K)]`. The branch had no effect -
    # TOP_K is 6, max(3, 6) is 6, and 6 is already the SQL LIMIT, so both arms returned the same
    # full list. Removed rather than converted into a real trim: trimming would change retrieval,
    # which is a tuning decision, not a cleanup.
    return hits
