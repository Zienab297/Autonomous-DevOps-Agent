from setup_path import *
"""
knowledge_core/retriever.py
---------------------------
Embeds incoming error message → searches Qdrant → returns RetrievalResult.

With Knowledge Graph:
  1. Graph finds related entry IDs + keywords
  2. Keywords enrich the query text
  3. Qdrant searches with enriched query
  4. Returns best match above threshold
"""

from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient

from shared.models import RetrievalResult
from shared.config import Config


def retrieve(
    error_message: str,
    client: QdrantClient,
    config: Config,
    graph=None,          # KnowledgeGraph — optional
) -> RetrievalResult:

    # ── step 1: enrich query using Knowledge Graph 
    if graph is not None:
        related_ids = graph.get_related_ids(error_message)

        # collect keywords from all related nodes
        extra_keywords = []
        for entry_id in related_ids:
            extra_keywords.extend(graph.get_keywords(entry_id))

        # deduplicate and build enriched query
        extra_keywords = list(dict.fromkeys(extra_keywords))
        enriched_query = error_message + " " + " ".join(extra_keywords)
        print(f"[Retriever] Enriched query with {len(extra_keywords)} keywords "
              f"from {len(related_ids)} related nodes")
    else:
        enriched_query = error_message

    # ── step 2: embed enriched query 
    model = SentenceTransformer("all-MiniLM-L6-v2")
    query_vector = model.encode(enriched_query).tolist()

    # ── step 3: search Qdrant 
    hits = client.query_points(
        collection_name=config.collection_name,
        query=query_vector,
        limit=config.top_k,
    ).points

    if not hits:
        return RetrievalResult(found=False, score=0.0)

    top   = hits[0]
    score = round(top.score, 4)

    print(f"[Retriever] Top match: {top.payload.get('id')} | score={score}")

    if score >= config.similarity_threshold:
        return RetrievalResult(found=True, score=score, entry=top.payload)

    return RetrievalResult(found=False, score=score)