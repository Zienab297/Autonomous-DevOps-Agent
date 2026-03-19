"""
test_knowledge_agent.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.config  import load_config
from shared.models  import RAGSource

from ingestion.pipeline             import run_pipeline
from knowledge_core.knowledge_agent import KnowledgeAgent
from knowledge_core.knowledge_graph import KnowledgeGraph


TEST_CASES = [
     {
        "name"  : "Case 2 -- Docker COPY error",
        "error" : "COPY failed: file not found in build context",
        "expect": RAGSource.KNOWLEDGE_BASE,
    },
    {
        "name"  : "Case 1 -- Terraform state lock (should use LLM fallback)",
        "error" : "Error locking state: Error acquiring the state lock: ConditionalCheckFailedException: The conditional request failed",
        "expect": RAGSource.LLM_GENERATED,
    },
    # {
    #     "name"  : "Case 2 -- Docker COPY error (should match KB)",
    #     "error" : "COPY failed: file not found in build context or excluded by .dockerignore: stat app/config.yaml: file does not exist",
    #     "expect": RAGSource.KNOWLEDGE_BASE,
    # },
    # {
    #     "name"  : "Case 3 -- Kubernetes CrashLoopBackOff (should match KB)",
    #     "error" : "Back-off restarting failed container: my-app-xyz has status CrashLoopBackOff restarts=8",
    #     "expect": RAGSource.KNOWLEDGE_BASE,
    # },
    # {
    #     "name"  : "Case 4 -- Hardcoded secret detected (should match KB)",
    #     "error" : "Hardcoded secret detected in code / secret scanning alert",
    #     "expect": RAGSource.KNOWLEDGE_BASE,
    # },
]


def print_separator(char="─", width=60):
    print(char * width)


def print_graph_section(error_message: str, graph: KnowledgeGraph):
    """Show what the Knowledge Graph found for this error."""
    print()
    print("  -- Knowledge Graph --")

    related_ids = graph.get_related_ids(error_message)

    if not related_ids:
        print("  No related nodes found")
        print("  --------------------")
        return

    print(f"  matched nodes  : {len(related_ids)}")
    for entry_id in related_ids:
        layer    = graph.get_layer(entry_id)
        keywords = graph.get_keywords(entry_id)
        print(f"  {entry_id:<15} | {layer:<30} | keywords: {', '.join(keywords[:3])}")

    all_keywords = []
    for entry_id in related_ids:
        all_keywords.extend(graph.get_keywords(entry_id))
    all_keywords = list(dict.fromkeys(all_keywords))
    print(f"  enriched query : +{len(all_keywords)} keywords added to search")
    print("  --------------------")
    print()


def print_response(response):
    print(f"  source     : {response.source.value}")
    print(f"  confidence : {response.confidence:.2f}")
    print(f"  category   : {response.category.value}")
    print(f"  action     : {response.action_needed}")

    # KB reference
    if response.source == RAGSource.KNOWLEDGE_BASE and response.rag_result:
        print()
        print("  -- KB Reference --")
        print(f"  entry_id      : {response.rag_result.entry_id}")
        print(f"  error_pattern : {response.rag_result.error_pattern}")
        print(f"  root_cause    : {response.rag_result.root_cause}")
        print("  ------------------")

    # web search references
    if response.source == RAGSource.LLM_GENERATED and response.web_sources:
        print()
        print("  -- Web Search References --")
        for i, url in enumerate(response.web_sources[:5], 1):
            print(f"  [{i}] {url}")
        print("  ---------------------------")

    # commands
    if response.suggested_commands:
        print()
        print("  commands:")
        for cmd in response.suggested_commands[:3]:
            print(f"    $ {cmd}")

    print()
    print("  -- Solution --")
    print("  " + response.healing_prompt.replace("\n", "\n  "))
    print("  --------------")


def main():
    print_separator("=")
    print("  Knowledge Agent -- End-to-End Test")
    print_separator("=")

    print("\n[SETUP] Running ingestion pipeline...")
    config = load_config()
    run_pipeline()
    print("[SETUP] Qdrant populated\n")

    agent  = KnowledgeAgent(config)
    graph  = agent.graph           # reuse the same graph instance
    passed = 0
    failed = 0

    for i, tc in enumerate(TEST_CASES, 1):
        print_separator()
        print(f"\n{tc['name']}")
        print(f"\n  error: {tc['error'][:80]}...")

        # ── show Knowledge Graph section ──────────────────────────────────
        print_graph_section(tc["error"], graph)

        # ── run agent ────────────────────────────────────────────────────
        response = agent.run(tc["error"])
        print_response(response)

        if response.source == tc["expect"]:
            print(f"  PASSED -- got expected source: {tc['expect'].value}")
            passed += 1
        else:
            print(f"  FAILED -- expected {tc['expect'].value}, got {response.source.value}")
            failed += 1

        print()

    print_separator("=")
    print(f"  Results: {passed} passed / {failed} failed / {len(TEST_CASES)} total")
    print_separator("=")

    if failed == 0:
        print("\n  All tests passed\n")
    else:
        print(f"\n  {failed} test(s) failed\n")


if __name__ == "__main__":
    main()