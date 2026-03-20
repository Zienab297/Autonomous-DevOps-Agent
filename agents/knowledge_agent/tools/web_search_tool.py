from setup_path import *
"""
tools/web_search_tool.py
------------------------
Web search tool used by llm_generator when Qdrant score < threshold.
Uses DuckDuckGo (free, no API key needed).
"""

from dataclasses import dataclass
from ddgs import DDGS


@dataclass
class SearchResult:
    title: str
    snippet: str
    url: str


def web_search(query: str, max_results: int = 5) -> list[SearchResult]:
    results = []

    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(SearchResult(
                title=r.get("title", ""),
                snippet=r.get("body", ""),
                url=r.get("href", ""),
            ))

    print(f"[WebSearch] '{query}' → {len(results)} results")
    return results


def format_results_for_prompt(results: list[SearchResult]) -> str:
    if not results:
        return "No web results found."

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.title}")
        lines.append(f"    {r.snippet}")
        lines.append(f"    Source: {r.url}")
        lines.append("")

    return "\n".join(lines)