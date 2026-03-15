"""
shared/config.py
--------------
Central configuration - loaded from .env file or environment variables.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# load .env automatically - searches current dir and all parent dirs
try:
    from dotenv import load_dotenv
    _dir = Path(__file__).resolve().parent
    while _dir != _dir.parent:
        if (_dir / ".env").exists():
            load_dotenv(_dir / ".env")
            break
        _dir = _dir.parent
except ImportError:
    pass  # dotenv not installed - will use system env vars


@dataclass
class Config:
    # Gemini
    gemini_api_key: str
    embedding_model: str = "all-MiniLM-L6-v2"
    generation_model: str = "llama3.2:3b"

    # Qdrant
    qdrant_host: str      = "localhost"
    qdrant_port: int      = 6333
    collection_name: str  = "devops_knowledge"

    # Retrieval
    similarity_threshold: float = 0.77
    top_k: int                  = 1

    # Ingestion
    embedding_batch_size: int   = 5
    embedding_sleep: float      = 1.0

    # Data
    knowledge_base_path: str    = "data/knowledge_base.json"


def load_config() -> Config:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY not found.\n"
            "Either:\n"
            "  1. Add it to your .env file: GEMINI_API_KEY=your_key\n"
            "  2. Or set it in terminal:    set GEMINI_API_KEY=your_key"
        )
    
    base_dir = Path(__file__).resolve().parent.parent  # agents/knowledge_agent/
    kb_path  = str(base_dir / "data" / "knowledge_base.json")

    return Config(
        gemini_api_key=api_key,
        qdrant_host=os.getenv("QDRANT_HOST", "localhost"),
        qdrant_port=int(os.getenv("QDRANT_PORT", "6333")),
        similarity_threshold=float(os.getenv("SIMILARITY_THRESHOLD", "0.77")),
        knowledge_base_path=kb_path,
    )