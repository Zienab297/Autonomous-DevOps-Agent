"""
ingestion/pipeline.py
---------------------
Runs all 5 ingestion steps in order.
Run this once to populate Qdrant before using the agent.

Usage:
    GEMINI_API_KEY=your_key python -m ingestion.pipeline
"""

from agents.knowledge_agent.shared.config import load_config
from agents.knowledge_agent.ingestion.loader       import load_knowledge_base
from agents.knowledge_agent.ingestion.parser       import parse_entries
from agents.knowledge_agent.ingestion.chunker      import chunk_entries
from agents.knowledge_agent.ingestion.embedder     import embed_chunks
from agents.knowledge_agent.ingestion.vector_store import build_vector_store


def run_pipeline():
    config = load_config()

    print("=" * 50)
    print("  Knowledge Agent — Ingestion Pipeline")
    print("=" * 50)

    print("\n[1/5] Loading...")
    entries = load_knowledge_base(config.knowledge_base_path)

    print("\n[2/5] Parsing...")
    parsed = parse_entries(entries)

    print("\n[3/5] Chunking...")
    chunks = chunk_entries(parsed)

    print("\n[4/5] Embedding...")
    embedded = embed_chunks(chunks)

    print("\n[5/5] Storing in Qdrant...")
    build_vector_store(embedded, config)

    print("\n" + "=" * 50)
    print("  Pipeline complete — Qdrant is ready")
    print("=" * 50)


if __name__ == "__main__":
    run_pipeline()