"""
ingestion/chunker.py
--------------------
Step 3 — ParsedEntry → Chunk.

1 chunk per entry (each entry is already small and focused).
This layer exists so smarter chunking can be added later
without touching the rest of the pipeline.
"""

from shared.models import ParsedEntry, Chunk


def chunk_entries(parsed_entries: list[ParsedEntry]) -> list[Chunk]:
    chunks = [
        Chunk(text=entry.text, metadata=entry.metadata)
        for entry in parsed_entries
    ]
    print(f"[Chunker] Created {len(chunks)} chunks")
    return chunks