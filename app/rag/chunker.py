"""Heading-aware chunker. 700 chars / 120 overlap - justified in DECISIONS.md (FR-1.3)."""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


@dataclass
class SourceDoc:
    title: str
    doc_type: str
    category: str
    source_ref: str
    body: str


@dataclass
class Chunk:
    index: int
    content: str
    category: str
    source_ref: str
    token_count: int


def parse_doc(path: Path) -> SourceDoc:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing frontmatter block")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    body = text[m.end():].strip()
    return SourceDoc(
        title=meta["title"], doc_type=meta["doc_type"],
        category=meta["category"], source_ref=meta["source_ref"], body=body,
    )


def _sections(body: str) -> list[str]:
    """Split on markdown headings first so a chunk rarely straddles two topics."""
    parts, current = [], []
    for line in body.splitlines():
        if line.startswith("# ") and current:
            parts.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append("\n".join(current).strip())
    return [p for p in parts if p]


def _pack(section: str, size: int, overlap: int) -> list[str]:
    """Pack a section into <=size pieces, never splitting a line (keeps bullets/table rows whole)."""
    if len(section) <= size:
        return [section]
    out, buf = [], ""
    for line in section.splitlines(keepends=True):
        if len(buf) + len(line) > size and buf:
            out.append(buf.strip())
            tail = buf[-overlap:]
            buf = tail.split("\n", 1)[-1] if "\n" in tail else ""
        buf += line
    if buf.strip():
        out.append(buf.strip())
    return out


def chunk_document(doc: SourceDoc) -> list[Chunk]:
    size, overlap = settings.CHUNK_CHARS, settings.CHUNK_OVERLAP
    pieces: list[str] = []
    for section in _sections(doc.body):
        pieces.extend(_pack(section, size, overlap))
    return [
        Chunk(index=i, content=p, category=doc.category,
              source_ref=doc.source_ref, token_count=max(1, len(p) // 4))
        for i, p in enumerate(pieces) if p.strip()
    ]


def load_corpus(directory: str | Path | None = None) -> list[tuple[SourceDoc, list[Chunk]]]:
    directory = Path(directory or Path(__file__).parent / "corpus")
    out = []
    for path in sorted(directory.glob("*.md")):
        doc = parse_doc(path)
        out.append((doc, chunk_document(doc)))
    return out
