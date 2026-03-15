"""
test_knowledge_agent.py
-----------------------
End-to-end test for the Knowledge Agent layer.

Before running:
  1. docker run -p 6333:6333 qdrant/qdrant
  2. export GEMINI_API_KEY=your_key
  3. python test_knowledge_agent.py

What this test does:
  - Runs the full ingestion pipeline (loads JSON -> Qdrant)
  - Tests 3 scenarios:
      Case 1 : error EXISTS in knowledge base  -> expects KB match
      Case 2 : error EXISTS in knowledge base  -> different category
      Case 3 : error DOES NOT exist in KB      -> expects LLM fallback
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.config  import load_config
from shared.models  import RAGSource

from ingestion.pipeline             import run_pipeline
from knowledge_core.knowledge_agent import KnowledgeAgent


# ── test cases ────────────────────────────────────────────────────────────────

TEST_CASES = [
    {
        "name"  : "Case 1 — Terraform state lock (should use LLM fallback)",
        "error" : "Error locking state: Error acquiring the state lock: ConditionalCheckFailedException: The conditional request failed",
        "expect": RAGSource.LLM_GENERATED,
    },
    # {
    #     "name"  : "Case 2 — Docker COPY error (should match KB)",
    #     "error" : "COPY failed: file not found in build context or excluded by .dockerignore: stat app/config.yaml: file does not exist",
    #     "expect": RAGSource.KNOWLEDGE_BASE,
    # },
    # {
    #     "name"  : "Case 3 — Kubernetes CrashLoopBackOff (should match KB)",
    #     "error" : "Back-off restarting failed container: my-app-xyz has status CrashLoopBackOff restarts=8",
    #     "expect": RAGSource.KNOWLEDGE_BASE,
    # },
    # {
    #     "name"  : "Case 4 — Hardcoded secret detected (should match KB)",
    #     "error" : "Hardcoded secret detected in code / secret scanning alert",
    #     "expect": RAGSource.KNOWLEDGE_BASE,
    # },
]


# ── helpers ───────────────────────────────────────────────────────────────────

def print_separator(char="─", width=60):
    print(char * width)


def print_response(response):
    print(f"  source     : {response.source.value}")
    print(f"  confidence : {response.confidence:.2f}")
    print(f"  category   : {response.category.value}")
    print(f"  action     : {response.action_needed}")
    if response.suggested_commands:
        print(f"  commands   :")
        for cmd in response.suggested_commands[:3]:
            print(f"    $ {cmd}")
    print()
    print("  healing_prompt (first 300 chars):")
    print("  " + response.healing_prompt[:300].replace("\n", "\n  "))
    print()
    print("  -- formatted response --")
    print("  " + response.healing_prompt.replace("\n", "\n  "))
    print("  ------------------------")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print_separator("=")
    print("  Knowledge Agent -- End-to-End Test")
    print_separator("=")

    print("\n[SETUP] Running ingestion pipeline...")
    config = load_config()
    run_pipeline()
    print("[SETUP] Qdrant populated\n")

    agent  = KnowledgeAgent(config)
    passed = 0
    failed = 0

    for i, tc in enumerate(TEST_CASES, 1):
        print_separator()
        print(f"\n{tc['name']}")
        print(f"\n  error: {tc['error'][:80]}...")
        print()

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