"""
ingestion/vector_store.py
-------------------------
Step 5 — EmbeddedChunk → Qdrant collection.

Each point:
  id      = index (0, 1, 2 ...)
  vector  = 768-dim float list
  payload = metadata (id, category, healing_prompt, tags ...)
"""

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from shared.models import EmbeddedChunk
from shared.config import Config


def build_vector_store(embedded_chunks: list[EmbeddedChunk], config: Config) -> QdrantClient:
    client = QdrantClient(host=config.qdrant_host, port=config.qdrant_port)
    vector_dim = len(embedded_chunks[0].vector)

    client.recreate_collection(
        collection_name=config.collection_name,
        vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
    )
    print(f"[VectorStore] Collection '{config.collection_name}' created (dim={vector_dim})")

    points = [
        PointStruct(id=idx, vector=chunk.vector, payload=chunk.metadata)
        for idx, chunk in enumerate(embedded_chunks)
    ]

    client.upsert(collection_name=config.collection_name, points=points)

    count = client.count(collection_name=config.collection_name).count
    print(f"[VectorStore] Upserted {count} points")
    return client