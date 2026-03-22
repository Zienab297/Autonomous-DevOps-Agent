"""
ingestion/parser.py
-------------------
Step 2 — Raw dict → ParsedEntry(text, metadata).

text     = error_pattern + keywords + root_cause  (used for embedding)
metadata = everything else                         (stored in Qdrant payload)
"""

from shared.models import ParsedEntry


def parse_entries(entries: list[dict]) -> list[ParsedEntry]:
    parsed = []

    for entry in entries:
        keywords_str = ", ".join(entry.get("error_keywords", []))

        text = (
            f"error: {entry.get('error_pattern', '')}\n"
            f"keywords: {keywords_str}\n"
            f"cause: {entry.get('root_cause', '')}"
        )

        metadata = {
            "id":             entry.get("id"),
            "category":       entry.get("category"),
            "subcategory":    entry.get("subcategory"),
            "error_pattern":  entry.get("error_pattern"),
            "error_example":  entry.get("error_example"),
            "healing_prompt": entry.get("healing_prompt"),
            "tags":           entry.get("tags", []),
        }

        parsed.append(ParsedEntry(text=text, metadata=metadata))

    print(f"[Parser] Parsed {len(parsed)} entries")
    return parsed