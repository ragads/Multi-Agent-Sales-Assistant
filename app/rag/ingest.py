"""One-shot ingestion: corpus -> chunks -> embeddings -> pgvector (FR-1.2 .. FR-1.6).

Usage:  python -m app.rag.ingest
Idempotent: re-running replaces documents by title.
"""
from __future__ import annotations
import asyncio, json

from openai import AsyncOpenAI

from app.config import settings
from app.rag.chunker import load_corpus
from app.state.store import store

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = await client.embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


async def main() -> None:
    await store.connect()
    corpus = load_corpus()
    print(f"{'document':46} {'chunks':>7} {'avg tokens':>11}")
    print("-" * 68)
    total = 0
    async with store.pool.acquire() as con:
        for doc, chunks in corpus:
            if not chunks:
                raise SystemExit(f"{doc.title} produced zero chunks - check the frontmatter/body")
            vectors = await embed_batch([c.content for c in chunks])
            if any(v is None or len(v) != settings.EMBEDDING_DIMS for v in vectors):
                raise SystemExit(f"{doc.title}: embedding failed or wrong dimension")

            async with con.transaction():
                await con.execute("delete from documents where title = $1", doc.title)
                doc_id = await con.fetchval(
                    """insert into documents (title, doc_type, source_ref, raw_content)
                       values ($1,$2,$3,$4) returning id""",
                    doc.title, doc.doc_type, doc.source_ref, doc.body,
                )
                for ch, vec in zip(chunks, vectors):
                    await con.execute(
                        """insert into chunks
                           (document_id, chunk_index, content, category, source_ref, token_count, embedding)
                           values ($1,$2,$3,$4,$5,$6,$7::vector)""",
                        doc_id, ch.index, ch.content, ch.category, ch.source_ref,
                        ch.token_count, vec_literal(vec),
                    )
            avg = sum(c.token_count for c in chunks) / len(chunks)
            total += len(chunks)
            print(f"{doc.title[:46]:46} {len(chunks):>7} {avg:>11.0f}")
    print("-" * 68)
    print(f"{'TOTAL':46} {total:>7}")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
