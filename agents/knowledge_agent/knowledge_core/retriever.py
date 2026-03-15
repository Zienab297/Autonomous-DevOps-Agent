from setup_path import *
"""
knowledge_core/retriever.py
---------------------------
Embeds incoming error message → searches Qdrant → returns RetrievalResult.
"""

#from google import genai
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient

from shared.models import RetrievalResult
from shared.config import Config


def retrieve(error_message: str, client: QdrantClient, config: Config) -> RetrievalResult:
    model = SentenceTransformer("all-MiniLM-L6-v2")
    query_vector = model.encode(error_message).tolist()

    hits = client.query_points(
        collection_name=config.collection_name,
        query=query_vector,
        limit=config.top_k,
    ).points

    if not hits:
        return RetrievalResult(found=False, score=0.0)

    top = hits[0]
    score = round(top.score, 4)

    print(f"[Retriever] Top match: {top.payload.get('id')} | score={score}")

    if score >= config.similarity_threshold:
        return RetrievalResult(found=True, score=score, entry=top.payload)

    return RetrievalResult(found=False, score=score)