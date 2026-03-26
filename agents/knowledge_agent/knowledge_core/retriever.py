from setup_path import *
"""
knowledge_core/retriever.py
---------------------------
System-aware retrieval:
  1. Knowledge Graph analyzes error → strategy + related IDs
  2. Strategy determines what to check first
  3. Enriched query → Qdrant search
"""

from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient

from agents.knowledge_agent.shared.models import RetrievalResult
from agents.knowledge_agent.shared.config import Config


def retrieve(
    error_message: str,
    client: QdrantClient,
    config: Config,
    graph=None,
) -> RetrievalResult:

    enriched_query = error_message

    if graph is not None:
        # ── step 1: graph analyzes error ──────────────────────────────────
        graph_result = graph.analyze(error_message)

        if graph_result.found:
            strategy = graph_result.strategy

            # ── step 2: apply strategy — check before searching ───────────
            if strategy.needs_context():
                _apply_strategy(strategy, error_message)

            # ── step 3: enrich query with related keywords ─────────────────
            if graph_result.keywords:
                enriched_query = error_message + " " + " ".join(graph_result.keywords)
                print(f"[Retriever] Enriched query with {len(graph_result.keywords)} keywords "
                      f"from {len(graph_result.related_ids)} related nodes")
        else:
            print(f"[Retriever] No graph match — using raw error message")

    # ── step 4: embed and search Qdrant ──────────────────────────────────
    model = SentenceTransformer("all-MiniLM-L6-v2")
    query_vector = model.encode(enriched_query).tolist()

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


def _apply_strategy(strategy, error_message: str) -> None:
    """
    Apply pre-search checks based on the strategy.
    Prints what the agent is checking before searching Qdrant.
    This is where future integrations (K8s API, deployment history) will plug in.
    """
    print(f"\n[Retriever] Applying strategy before search:")
    print(f"  Reasoning: {strategy.reasoning}")

    if strategy.check_deployments:
        print(f"  [Strategy] Checking recent deployments...")
        # TODO: query deployment history from StateManager or CI/CD agent

    if strategy.check_service_health:
        print(f"  [Strategy] Checking service health and dependencies...")
        # TODO: query service mesh or Kubernetes API

    if strategy.check_resources:
        print(f"  [Strategy] Checking cluster resources...")
        # TODO: query Prometheus or kubectl top nodes

    if strategy.check_secrets:
        print(f"  [Strategy] Checking secrets and configuration...")
        # TODO: query Kubernetes secrets or vault

    if strategy.search_order:
        print(f"  [Strategy] Search order: {' → '.join(strategy.search_order)}")