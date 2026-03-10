from setup_path import *
"""
ingestion/embedder.py
---------------------
Step 4 — Chunk → EmbeddedChunk(vector, text, metadata).
"""

#from google import genai # for use gemini embedding 
from sentence_transformers import SentenceTransformer
from core.models import Chunk, EmbeddedChunk
from core.config import Config



def embed_chunks(chunks: list[Chunk]) -> list[EmbeddedChunk]:
    model = SentenceTransformer("all-MiniLM-L6-v2")
    embedded = []

    for chunk in chunks:
        vector = model.encode(chunk.text).tolist()
        embedded.append(EmbeddedChunk(
            vector=vector,
            text=chunk.text,
            metadata=chunk.metadata,
        ))

    print(f"[Embedder] Done — {len(embedded)} vectors, dim={len(embedded[0].vector)}")
    return embedded