"""
knowledge_core/knowledge_graph.py
----------------------------------
Loads the knowledge graph and determines:
  1. Which entry IDs are related to the error
  2. What strategy to use before searching Qdrant

Strategy drives system-aware reasoning:
  - check_deployments  → ask about recent deployments first
  - check_service_health → check service dependencies first
  - check_resources    → check cluster resources first
  - check_secrets      → check secrets/config first
"""

import json
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class SearchStrategy:
    """Defines what to check before searching Qdrant."""
    check_deployments:    bool       = False
    check_service_health: bool       = False
    check_resources:      bool       = False
    check_secrets:        bool       = False
    search_order:         list[str]  = field(default_factory=list)
    reasoning:            str        = ""

    def needs_context(self) -> bool:
        """Returns True if any pre-search check is needed."""
        return any([
            self.check_deployments,
            self.check_service_health,
            self.check_resources,
            self.check_secrets,
        ])

    def summary(self) -> str:
        checks = []
        if self.check_deployments:    checks.append("deployments")
        if self.check_service_health: checks.append("service health")
        if self.check_resources:      checks.append("resources")
        if self.check_secrets:        checks.append("secrets")
        return f"Check: {', '.join(checks) if checks else 'none'} | Order: {self.search_order}"


@dataclass
class GraphResult:
    """Full result from the Knowledge Graph lookup."""
    matched_node:  str            = ""
    match_score:   int            = 0
    related_ids:   list[str]      = field(default_factory=list)
    keywords:      list[str]      = field(default_factory=list)
    strategy:      SearchStrategy = field(default_factory=SearchStrategy)
    found:         bool           = False


class KnowledgeGraph:

    def __init__(self, graph_path: str = None):
        if graph_path is None:
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

    def analyze(self, error_message: str, max_related: int = 3) -> GraphResult:
        """
        Main method — given an error message:
          1. Find the best matching node
          2. Get related entry IDs
          3. Get the search strategy
          4. Return GraphResult
        """
        error_lower = error_message.lower()

        # ── find best matching node ───────────────────────────────────────
        best_node_id = None
        best_score   = 0

        for node_id, node in self.nodes.items():
            keywords = node.get("keywords", [])
            score = sum(1 for kw in keywords if kw.lower() in error_lower)
            if score > best_score:
                best_score   = score
                best_node_id = node_id

        if not best_node_id or best_score == 0:
            print(f"[KnowledgeGraph] No match found")
            return GraphResult(found=False)

        print(f"[KnowledgeGraph] Matched: {best_node_id} (score={best_score})")

        node = self.nodes[best_node_id]

        # ── collect related IDs ───────────────────────────────────────────
        related = node.get("related", [])
        causes  = node.get("causes",  [])

        all_ids = [best_node_id] + related + causes
        seen, result = set(), []
        for id_ in all_ids:
            if id_ not in seen:
                seen.add(id_)
                result.append(id_)
            if len(result) >= max_related:
                break

        # ── collect all keywords from related nodes ───────────────────────
        all_keywords = []
        for id_ in result:
            all_keywords.extend(self.nodes.get(id_, {}).get("keywords", []))
        all_keywords = list(dict.fromkeys(all_keywords))

        # ── build strategy ────────────────────────────────────────────────
        strategy_data = node.get("strategy", {})
        strategy = SearchStrategy(
            check_deployments    = strategy_data.get("check_deployments", False),
            check_service_health = strategy_data.get("check_service_health", False),
            check_resources      = strategy_data.get("check_resources", False),
            check_secrets        = strategy_data.get("check_secrets", False),
            search_order         = strategy_data.get("search_order", []),
            reasoning            = strategy_data.get("reasoning", ""),
        )

        print(f"[KnowledgeGraph] Strategy: {strategy.summary()}")
        print(f"[KnowledgeGraph] Reasoning: {strategy.reasoning}")

        return GraphResult(
            matched_node = best_node_id,
            match_score  = best_score,
            related_ids  = result,
            keywords     = all_keywords,
            strategy     = strategy,
            found        = True,
        )

    def get_layer(self, entry_id: str) -> str:
        return self.nodes.get(entry_id, {}).get("layer", "Unknown")

    def get_keywords(self, entry_id: str) -> list[str]:
        return self.nodes.get(entry_id, {}).get("keywords", [])

    def get_strategy(self, entry_id: str) -> SearchStrategy:
        strategy_data = self.nodes.get(entry_id, {}).get("strategy", {})
        return SearchStrategy(
            check_deployments    = strategy_data.get("check_deployments", False),
            check_service_health = strategy_data.get("check_service_health", False),
            check_resources      = strategy_data.get("check_resources", False),
            check_secrets        = strategy_data.get("check_secrets", False),
            search_order         = strategy_data.get("search_order", []),
            reasoning            = strategy_data.get("reasoning", ""),
        )