"""
ingestion/loader.py
-------------------
Step 1 — Load knowledge_base.json → list of raw dicts.
"""

import json
from pathlib import Path


def load_knowledge_base(json_path: str) -> list[dict]:
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"Knowledge base not found: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("entries", [])
    print(f"[Loader] Loaded {len(entries)} entries from {json_path}")
    return entries