from setup_path import *
"""
ingestion/embedder.py
---------------------
Step 4 — Chunk → EmbeddedChunk(vector, text, metadata).
"""

import time
from google import genai

from core.models import Chunk, EmbeddedChunk
from core.config import Config


def embed_chunks(chunks: list[Chunk], config: Config) -> list[EmbeddedChunk]:
    client = genai.Client(api_key=config.gemini_api_key)
    embedded: list[EmbeddedChunk] = []

    for i in range(0, len(chunks), config.embedding_batch_size):
        batch = chunks[i : i + config.embedding_batch_size]

        print(f"[Embedder] Batch {i // config.embedding_batch_size + 1} "
              f"({len(batch)} chunks)...")

        for chunk in batch:
            result = client.models.embed_content(
                model=config.embedding_model,
                contents=chunk.text,
            )
            vector = result.embeddings[0].values
            embedded.append(EmbeddedChunk(
                vector=vector,
                text=chunk.text,
                metadata=chunk.metadata,
            ))

        if i + config.embedding_batch_size < len(chunks):
            time.sleep(config.embedding_sleep)

    print(f"[Embedder] Done — {len(embedded)} vectors, dim={len(embedded[0].vector)}")
    return embedded