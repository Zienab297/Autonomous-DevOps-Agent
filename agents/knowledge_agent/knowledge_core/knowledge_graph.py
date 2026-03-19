
"""
knowledge_core/knowledge_graph.py
----------------------------------
Loads the knowledge graph and finds related error entries
for a given error message.

Usage:
    graph = KnowledgeGraph()
    related_ids = graph.get_related_ids("CrashLoopBackOff in my-app")
    # returns: ["K8S-001", "K8S-002", "K8S-008", "K8S-009"]
"""

import json
from pathlib import Path


class KnowledgeGraph:

    def __init__(self, graph_path: str = None):
        if graph_path is None:
            # find data/knowledge_graph.json relative to knowledge_agent root
            _current = Path(__file__).resolve().parent
            while _current != _current.parent:
                if _current.name == "knowledge_agent":
                    graph_path = str(_current / "data" / "knowledge_graph.json")
                    break
                _current = _current.parent

        with open(graph_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.nodes: dict = data.get("nodes", {})
        print(f"[KnowledgeGraph] Loaded {len(self.nodes)} nodes")

    def get_related_ids(self, error_message: str, max_results: int = 3) -> list[str]:
        """
        Given an error message, find the matching node in the graph
        and return its related entry IDs.

        Steps:
          1. Match error message to a node using keywords
          2. Return that node + its related + its causes
        """
        error_lower = error_message.lower()

        # ── step 1: find the best matching node ──────────────────────────
        best_node_id = None
        best_score   = 0

        for node_id, node in self.nodes.items():
            keywords = node.get("keywords", [])
            score = sum(1 for kw in keywords if kw.lower() in error_lower)
            if score > best_score:
                best_score   = score
                best_node_id = node_id

        if not best_node_id or best_score == 0:
            print(f"[KnowledgeGraph] No match found for error")
            return []

        print(f"[KnowledgeGraph] Matched node: {best_node_id} (score={best_score})")

        # ── step 2: collect related IDs ───────────────────────────────────
        node    = self.nodes[best_node_id]
        related = node.get("related", [])
        causes  = node.get("causes",  [])

        # merge: matched node + related + causes, deduplicated
        all_ids = [best_node_id] + related + causes
        seen    = set()
        result  = []
        for id_ in all_ids:
            if id_ not in seen:
                seen.add(id_)
                result.append(id_)
            if len(result) >= max_results:
                break

        print(f"[KnowledgeGraph] Related IDs: {result}")
        return result

    def get_layer(self, entry_id: str) -> str:
        """Return the deployment layer for a given entry ID."""
        node = self.nodes.get(entry_id, {})
        return node.get("layer", "Unknown")

    def get_keywords(self, entry_id: str) -> list[str]:
        """Return the keywords for a given entry ID."""
        node = self.nodes.get(entry_id, {})
        return node.get("keywords", [])